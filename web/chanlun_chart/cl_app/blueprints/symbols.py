"""
多市场标的列表浏览蓝图。

提供以下接口：
- GET  /symbols                       : 渲染独立的"标的列表"页面
- GET  /symbols/list                  : JSON 数据接口，按市场返回（可选）模糊搜索 + 分页后的标的
- POST /symbols/prewarm               : 启动当前市场全量缠论数据预热（后台串行执行）
- GET  /symbols/prewarm/status        : 查询当前市场最新一次预热任务的进度
- POST /symbols/prewarm/cancel        : 取消当前市场正在执行的预热任务

实现复用 tv 蓝图中已有的 ``get_cached_processed_stocks`` 缓存能力，
不重复触发交易所连接，符合启动期预加载与异步刷新策略。

预热实现要点（2026-04 重构后）：
- 同一时刻全局只允许 1 个市场在预热（避免多市场互相冲击）；
- 在该市场内：**多标的并行 + 单标的内 2 周期并行**（吞吐与连接池平衡）：
  - 多标的并行度按市场配置（a 股 xtquant 必须 1，us/hk 等 HTTP 数据源 2-3）；
  - 单标的内 4 周期固定用 2 个线程并行（PREWARM_FREQ_PARALLELISM=2，
    见该常量注释：实测 4 并发会打爆长桥连接池，2 是 throughput 与稳定性平衡点）；
  - 总并发上限受 chart_calc_locks + INFLIGHT_SEMAPHORE 共同约束。
- 计算结果直接写入 ``tv.chart_data_cache``，与用户实际查看图表时的 cache_key 完全一致，
  之后切换标的命中缓存可秒开；
- 用户最近看过的标的优先插队（每批调度时重排剩余 pending）；
- 任务对象内存维护，TTL 1 小时自动清理（避免页面长期不刷导致泄漏）。
"""

import json
import os
import pathlib
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional


# M3: env 优先的常量读取助手；解析失败兜底默认值，不让坏 env 炸服务启动。
def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)))
    except (TypeError, ValueError):
        return default

from flask import Blueprint, jsonify, render_template, request
from flask_login import login_required

from chanlun.cl_utils import query_cl_chart_config
from chanlun.tools.log_util import LogUtil

from ..services.chart_cache import (
    _build_cache_key,
    _get_chart_cache_entry,
    cache_lock,
    chart_data_cache,
)
from ..services.chart_compute import compute_and_cache_chart_data
from ..services.constants import market_types, resolution_maps
from ..services.prewarm_status import mark_batch_prewarm_active
from ..services.stock_list import get_cached_processed_stocks
from ..services.user_activity import (
    _get_last_user_request_time,
    _get_user_recent_codes,
)

# 经 L1 + Tier 4 全套重构后，symbols.py 不再 import 任何 .tv 符号；
# 全部依赖均从 services 引入，蓝图与蓝图之间无任何越界耦合。

symbols_bp = Blueprint("symbols", __name__)

# 与 templates/index.html 顶部市场下拉保持一致的展示顺序与文案
MARKETS = [
    ("a", "沪深A股"),
    ("hk", "港股"),
    ("futures", "国内期货"),
    ("ny_futures", "纽约期货"),
    ("fx", "外汇"),
    ("us", "美股"),
    ("currency", "数字货币(合约)"),
    ("currency_spot", "数字货币(现货)"),
]

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 500

# 标的列表"仅显示个股"的 type 白名单。市场不在表中则不过滤。
# 港股/美股因数据源不返回 type 字段，本期不过滤；期货/外汇/数字货币本无个股概念。
STOCK_ONLY_TYPES_BY_MARKET: Dict[str, set] = {
    "a": {"stock_cn"},
}

# 美股 name 黑名单(数据源不返回 type 字段, 用 name 推断非股票).
# ETF/ETN 是缩写 -> 子串包含(普通股票名不会出现, 也能命中 '场ETF'/'ingETF' 这种粘连写法);
# Fund/Warrant/... 是英文词 -> \b 边界(避免误删 Foundation/warranty 等).
# 保留 Trust(REIT 算股票) 和 Index(用户可能想看指数), 如需扩展直接加词.
US_NON_STOCK_SUBSTRINGS = ("ETF", "ETN")
US_NON_STOCK_WORD_RE = re.compile(
    r"\b(Fund|Funds|Warrant|Warrants|Right|Rights|Unit|Units|Note|Notes)\b",
    re.IGNORECASE,
)


def _is_us_non_stock(name: str) -> bool:
    upper = name.upper()
    return (
        any(s in upper for s in US_NON_STOCK_SUBSTRINGS)
        or US_NON_STOCK_WORD_RE.search(name) is not None
    )


def _apply_market_filter(market: str, all_stocks: List[dict]) -> List[dict]:
    """对标的列表应用市场过滤(type 白名单 + 美股 name 黑名单).

    供 symbols_list (展示) 和 symbols_prewarm (预热) 共用, 确保
    预热范围与用户能搜到的标的一致, 不浪费 CPU/磁盘算 ETF 等.
    """
    allow_types = STOCK_ONLY_TYPES_BY_MARKET.get(market)
    if allow_types is not None:
        # 未带 type 或 'unknown' 默认放行, 避免把识别失败的当垃圾删掉.
        all_stocks = [
            s for s in all_stocks
            if (s.get("type", "unknown") in allow_types or s.get("type", "unknown") == "unknown")
        ]
    if market == "us":
        all_stocks = [s for s in all_stocks if not _is_us_non_stock(s.get("name", ""))]
    return all_stocks


@symbols_bp.route("/symbols")
@login_required
def symbols_page():
    """渲染标的列表页面。"""
    default_market = (request.args.get("market") or "a").strip().lower()
    if default_market not in market_types:
        default_market = "a"
    return render_template(
        "symbols.html",
        markets=MARKETS,
        default_market=default_market,
    )


@symbols_bp.route("/symbols/list")
@login_required
def symbols_list():
    """按市场返回标的列表（支持模糊搜索 + 分页）。

    Query 参数：
    - market    : 市场代码（必填，必须在 ``market_types`` 中）
    - q         : 模糊关键词，匹配 code / name / 拼音首字母（可选）
    - page      : 1-based 页码，默认 1
    - page_size : 每页数量，默认 50，上限 500
    """
    market = (request.args.get("market") or "").strip().lower()
    if market not in market_types:
        return jsonify({"ok": False, "msg": f"未知市场: {market!r}"}), 400

    query = (request.args.get("q") or "").strip().lower()

    # ``all=1`` 表示前端一次性拉全量（用于本地过滤+键盘连续浏览体验，
    # 避免分页造成键盘 ↑/↓ 在边界处中断）。其它情况保留分页兼容。
    return_all = (request.args.get("all") or "").strip() in ("1", "true", "yes")

    try:
        page = int(request.args.get("page", "1"))
    except (TypeError, ValueError):
        page = 1
    if page < 1:
        page = 1

    try:
        page_size = int(request.args.get("page_size", str(DEFAULT_PAGE_SIZE)))
    except (TypeError, ValueError):
        page_size = DEFAULT_PAGE_SIZE
    if page_size < 1:
        page_size = DEFAULT_PAGE_SIZE
    if page_size > MAX_PAGE_SIZE:
        page_size = MAX_PAGE_SIZE

    # allow_sync_fallback=True：用户在该页主动等待数据是合理的，宁可慢几秒也不能 500。
    try:
        all_stocks = get_cached_processed_stocks(market, allow_sync_fallback=True) or []
    except Exception as e:
        LogUtil.error(f"[symbols_list] get stocks failed market={market}: {e}")
        all_stocks = []

    # 应用市场过滤(与预热路径共用同一函数, 确保展示列表和预热范围一致)
    all_stocks = _apply_market_filter(market, all_stocks)

    if query:
        filtered = [
            s
            for s in all_stocks
            if query in s.get("code_lower", "")
            or query in s.get("name_lower", "")
            or query in s.get("pinyin_initials", "")
        ]
    else:
        filtered = all_stocks

    total = len(filtered)

    if return_all:
        page_items = filtered
        page = 1
        page_size = total
    else:
        start = (page - 1) * page_size
        end = start + page_size
        page_items = filtered[start:end]

    market_type = market_types.get(market, "")

    items = [
        {
            "code": s.get("code", ""),
            "name": s.get("name", ""),
            "pinyin": s.get("pinyin_initials", ""),
            "type": market_type,
        }
        for s in page_items
    ]

    return jsonify(
        {
            "ok": True,
            "market": market,
            "total": total,
            "page": page,
            "page_size": page_size,
            "items": items,
        }
    )

# ---------------------------------------------------------------------------
# 缠论数据预热（Pre-warm）
# ---------------------------------------------------------------------------

# 预热使用的常用周期（TV interval 表示法），通过 resolution_maps 转成项目内部 freq：
# "1D" -> d, "30" -> 30m, "5" -> 5m, "1" -> 1m
# 顺序仍然按用户最常看的优先（虽然现在并行计算，但失败时按顺序记录日志看起来更直观）。
PREWARM_INTERVALS = ["1D", "30", "5", "1"]

# 单标的内并发计算的周期数（每个周期一个线程）。
# 2026-04 调优：4 → 2。原因：实测全市场预热同时打 8 个并发请求时（2 标的 × 4 周期），
# 长桥 HTTP 连接池被占满，前端 polling 请求（每 3 秒 1 次/面板，4 面板 = 1.3 QPS）被排到
# 队列后面，导致已切换标的的旧请求耗时 10-18 秒，用户感觉切换很卡。
# 维持 2，让总在飞请求数 ≤ INFLIGHT_LIMIT，避免 4 周期同时抢一把信号量。
# M3: env PREWARM_FREQ_PARALLELISM 可覆盖
PREWARM_FREQ_PARALLELISM = _env_int("PREWARM_FREQ_PARALLELISM", 2)

# 多个标的之间并行处理的 worker 数。按市场区分：
# - a 股 xtquant native 不是线程安全的，必须串行 → 1
# - 其他 native 数据源（tdx 期货）也保险串行 → 1
# - HTTP 数据源（长桥/futu）放开并行：
#   2026-04 二次调优（M2 落盘后）：1 → 3 (us) / 1 → 2 (hk)。
#   现在用户切到已预热标的命中 disk，毫秒级返回；批量预热可以更激进抢占数据源
#   也不会再让用户体验崩。总在飞 = code_parallelism × freq_parallelism，但实际受
#   下面 INFLIGHT_LIMIT 全局信号量约束，不会无限叠加。
PREWARM_CODE_PARALLELISM_BY_MARKET = {
    "a": 1,            # xtquant native，绝对串行
    "futures": 1,      # tdx native，保险串行
    "ny_futures": 1,   # 同上
    "us": 3,           # 长桥 HTTP，可并行 3 个标的（M2 后允许更激进）
    "hk": 2,           # futu HTTP，可并行 2 个标的
    "fx": 1,
    "currency": 1,
    "currency_spot": 1,
}
PREWARM_CODE_PARALLELISM_DEFAULT = 1

# ⚠️ 关键：全局在飞请求数信号量上限。
# 这是单个 worker 进程内预热可同时打到数据源的最大请求数。
# 注意：threading.Semaphore 是**进程内**真理，不跨进程；本服务由 app.py 单进程启动
# （见 app.py:87-92 严禁多进程部署的注释），多 worker 部署下此上限会被乘 N 失效。
# 留出余量给用户的实时请求（用户的 tv_history 不走这个信号量，永远优先）。
# 2026-04 二次调优（M2 落盘后）：2 → 6。
# 长桥/futu 单连接池 QPS 上限实测 ~10，预热占 6，给用户实时请求和 polling 留 4 个余量。
# 用户切已预热标的现在走 disk hit 不再走 HTTP，所以可以放心吃满。
# M3: env PREWARM_GLOBAL_INFLIGHT_LIMIT 可覆盖
PREWARM_GLOBAL_INFLIGHT_LIMIT = _env_int("PREWARM_GLOBAL_INFLIGHT_LIMIT", 6)

# 用户活跃度让位：用户最近 N 秒内有 firstDataRequest=true 的请求时，预热请求等一下再发，
# 避免把用户的实时请求挤到 HTTP 连接队列后面。
# M3: env PREWARM_USER_ACTIVE_WINDOW_SECONDS 可覆盖
PREWARM_USER_ACTIVE_WINDOW_SECONDS = _env_float("PREWARM_USER_ACTIVE_WINDOW_SECONDS", 3.0)
# 让位等待时间（秒）。用户活跃时，预热请求会 sleep 这么久后再继续。
# 2026-04 二次调优：1.0 → 0.3。1s 让位过长，用户没切其它标的时也会因为单次 first=true
# 把后续预热堵 5 秒，全市场预热被腰斩。0.3s 既能让用户突发请求优先，又不会浪费太多。
# M3: env PREWARM_YIELD_SLEEP_SECONDS 可覆盖
PREWARM_YIELD_SLEEP_SECONDS = _env_float("PREWARM_YIELD_SLEEP_SECONDS", 0.3)

# 任务对象保留时长：完成后超过此时间允许新任务启动，并允许 GC。
PREWARM_TASK_RETAIN_SECONDS = 3600
# 用户最近请求过的标的优先插队：worker 在每轮循环开始时，会把还没预热的"用户最近看过"
# 的标的提到队首。

# 任务进度持久化目录与写盘频率
_PREWARM_PERSIST_DIRNAME = "prewarm_status"
# 写盘频率：每完成 N 个标的写一次（额外终态时强制写一次）。
# 50 是经验值：典型 11k 标的预热 220 次写盘，IO 开销 < 1%；同时崩溃后丢失进度 < 50 个。
# M3: env PREWARM_PERSIST_EVERY_N_DONE 可覆盖
_PREWARM_PERSIST_EVERY_N_DONE = _env_int("PREWARM_PERSIST_EVERY_N_DONE", 50)
# Resume 用的"已完成 code 列表"文件后缀：
# - 每完成一个 code（无论成功/失败）即追加一行，进程崩溃/取消后下次 start() 跳过；
# - 仅在任务 status==finished（全市场跑完）时删除，cancel/aborted/error 都保留以便续跑；
# - 文件格式为 plain text，每行一个 code，依赖 'a' mode + GIL 实现单进程多线程的写并发。
_PREWARM_DONE_SUFFIX = "_done.txt"

# 全局信号量：所有预热请求（不论标的不论周期）都要先 acquire 才能发请求。
# 这是防止打爆数据源的核心机制。
_PREWARM_INFLIGHT_SEMAPHORE = threading.Semaphore(PREWARM_GLOBAL_INFLIGHT_LIMIT)

# M1: 启动速率限制。同一 market 在 N 秒内只允许成功启动 1 次预热，
# 防止用户脚本反复 POST /symbols/prewarm 滥用 CPU/磁盘/数据源 QPS。
# 设为 0 表示禁用速率限制。env PREWARM_RATE_LIMIT_SECONDS 可覆盖。
PREWARM_RATE_LIMIT_SECONDS = _env_int("PREWARM_RATE_LIMIT_SECONDS", 300)
# market -> last successful start ts；进程内字典，单 worker 假设下足够。
_prewarm_last_start_at: Dict[str, float] = {}
_prewarm_rate_lock = threading.Lock()

class PrewarmTask:
    """单次预热任务的进度对象（线程安全；通过 manager 的锁外部串行化访问）。"""

    __slots__ = (
        "task_id",
        "market",
        "total",
        "done",
        "succeeded",
        "failed",
        "current",
        "status",
        "started_at",
        "finished_at",
        "cancel_event",
        "error_msg",
        "persist_fail_count",
        "resumed_skipped",
    )

    def __init__(self, market: str, total: int):
        self.task_id: str = uuid.uuid4().hex
        self.market: str = market
        self.total: int = int(total)
        self.done: int = 0
        self.succeeded: int = 0
        self.failed: int = 0
        # (current_code, current_name) 一对原子写：避免多 worker 并发裸写两个独立属性
        # 时被读端观察到 (新 code, 旧 name) 撕裂组合。
        self.current: tuple = ("", "")
        self.status: str = "running"  # running | finished | cancelled | error
        self.started_at: float = time.time()
        self.finished_at: Optional[float] = None
        self.cancel_event: threading.Event = threading.Event()
        self.error_msg: str = ""
        # 连续持久化失败计数（运行时；不持久化跨重启）。≥3 次时升级为 ERROR + 写 error_msg。
        self.persist_fail_count: int = 0
        # M5 resume：本任务因为续跑而跳过的 code 数（仅展示用，total 已扣除）
        self.resumed_skipped: int = 0

    def to_dict(self) -> dict:
        cur_code, cur_name = self.current
        return {
            "task_id": self.task_id,
            "market": self.market,
            "total": self.total,
            "done": self.done,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "current_code": cur_code,
            "current_name": cur_name,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed": (self.finished_at or time.time()) - self.started_at,
            "error_msg": self.error_msg,
            "resumed_skipped": self.resumed_skipped,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PrewarmTask":
        """从持久化 json 还原任务对象（仅恢复展示字段，不恢复 cancel_event 等运行时对象）。"""
        t = cls(market=d["market"], total=int(d.get("total", 0)))
        t.task_id = d.get("task_id", t.task_id)
        t.done = int(d.get("done", 0))
        t.succeeded = int(d.get("succeeded", 0))
        t.failed = int(d.get("failed", 0))
        t.current = (d.get("current_code", ""), d.get("current_name", ""))
        t.status = d.get("status", "aborted")
        t.started_at = float(d.get("started_at", time.time()))
        finished = d.get("finished_at")
        t.finished_at = float(finished) if finished is not None else None
        t.error_msg = d.get("error_msg", "")
        t.resumed_skipped = int(d.get("resumed_skipped", 0))
        return t

class PrewarmManager:
    """全局单例，按 market 维度管理预热任务。

    并发约束（L3 重构后）：
    - **互斥粒度按"数据源 group"细化**：同一底层 exchange（如长桥同时承担 us+hk）
      内的 markets 互斥；不同 group 可并行预热。
      group 名取自 ``config.EXCHANGE_<MARKET>``（如 a→qmt、us/hk→cq、futures→tdx_futures）。
    - 全市场预热实测耗时从顺序 ~3h 降到并行 ~1-1.5h（取决于 group 划分）。
    - xtquant native 线程不安全的约束仍然成立，但只影响共享 xtquant 的 markets，
      不再阻断 HTTP 数据源（cq/binance/futu）的并行启动。
    - 每个 market 只保留最近一次任务（覆盖更早的）。
    """

    def __init__(self):
        self._lock = threading.Lock()
        # market -> 最近一次任务（无论是否已完成）
        self._tasks: Dict[str, PrewarmTask] = {}
        # L3: per-group 互斥状态（group → 当前在跑的 market）。
        # 同一 group 内严格互斥；不同 group 可并发。
        self._running_groups: Dict[str, str] = {}
        # worker 线程引用（仅用于调试，不主动 join）
        self._worker_thread: Optional[threading.Thread] = None
        # 启动恢复：从磁盘载入历史 task；进程内首次构造时执行一次。
        self._load_persisted_tasks()

    # ---------------- L3: 数据源分组 ----------------

    @staticmethod
    def _market_group(market: str) -> str:
        """根据 ``config.EXCHANGE_<MARKET>`` 推断锁分组。

        同一底层 exchange 实现（如长桥同时承担 us+hk、tdx 系列承担多个国内市场）
        共享一把锁，避免连接池/account session 撞车；不同实现可并行预热。

        Examples（按用户实际 config）:
        - EXCHANGE_A='qmt'           → group 'qmt'
        - EXCHANGE_HK='cq'           → group 'cq'  (与 us 同 group 互斥)
        - EXCHANGE_US='cq'           → group 'cq'
        - EXCHANGE_FUTURES='tdx_futures' → group 'tdx_futures'
        - EXCHANGE_NY_FUTURES='tdx_ny_futures' → group 'tdx_ny_futures'
        - EXCHANGE_FX='tdx_fx'       → group 'tdx_fx'
        - EXCHANGE_CURRENCY='binance' → group 'binance'
        - EXCHANGE_CURRENCY_SPOT='binance_spot' → group 'binance_spot'
        """
        from chanlun import config
        attr = f"EXCHANGE_{market.upper()}"
        value = getattr(config, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
        # fallback：未配置时按 market 名独立成组（保守，不与其他 market 互斥）
        return market.lower()

    # ---------------- 持久化 ----------------

    def _load_persisted_tasks(self) -> None:
        """启动时扫描 prewarm_status/*.json 还原 _tasks。
        - 损坏文件：warning + unlink + 跳过；
        - status 仍为 "running"：说明上次进程异常退出，改写为 "aborted" 并落盘。
        """
        d = self._persist_dir()
        if d is None:
            return
        try:
            files = list(d.glob("*.json"))
        except OSError as e:
            LogUtil.warning(f"[prewarm] list persist dir failed: {e}")
            return
        for path in files:
            try:
                raw = path.read_text(encoding="utf-8")
                data = json.loads(raw)
                task = PrewarmTask.from_dict(data)
            except (OSError, ValueError, KeyError) as e:
                LogUtil.warning(f"[prewarm] load persisted task corrupt path={path} err={e}, 删除")
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                continue
            if task.status == "running":
                # 上次进程异常退出，改为 aborted 并立即写回
                task.status = "aborted"
                if task.finished_at is None:
                    task.finished_at = time.time()
                try:
                    path.write_text(
                        json.dumps(task.to_dict(), ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                except OSError as e:
                    LogUtil.warning(f"[prewarm] write back aborted state failed: {e}")
            self._tasks[task.market] = task
            LogUtil.info(
                f"[prewarm] restored task market={task.market} status={task.status} "
                f"{task.done}/{task.total}"
            )

    def _persist_dir(self) -> "pathlib.Path":
        """惰性获取持久化目录；首次调用时创建。失败返回 None 由调用方降级。"""
        from chanlun.config import get_data_path
        try:
            d = get_data_path() / _PREWARM_PERSIST_DIRNAME
            d.mkdir(parents=True, exist_ok=True)
            return d
        except OSError as e:
            LogUtil.warning(f"[prewarm] persist dir create failed: {e}")
            return None

    # ---------------- M5 resume：已完成 code 列表 ----------------

    def _done_file_path(self, market: str) -> Optional["pathlib.Path"]:
        d = self._persist_dir()
        if d is None:
            return None
        return d / f"{market}{_PREWARM_DONE_SUFFIX}"

    def _load_done_codes(self, market: str) -> set:
        """从 <market>_done.txt 加载已完成 code 集合，损坏文件按空集处理（容错）.

        捕获范围说明：
        - OSError：文件被锁/权限异常等。
        - ValueError：覆盖 UnicodeDecodeError（继承自 ValueError），磁盘半截写入
          / 断电等导致 UTF-8 截断时不会让 start() 整个 500。
        """
        path = self._done_file_path(market)
        if path is None or not path.is_file():
            return set()
        try:
            return {
                line.strip()
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            }
        except (OSError, ValueError) as e:
            LogUtil.warning(f"[prewarm] load done codes failed market={market}: {e}")
            return set()

    def _clear_done_codes(self, market: str) -> None:
        """任务完整跑完时调用，删除 done 文件让下一次预热从头开始。"""
        path = self._done_file_path(market)
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError as e:
            LogUtil.warning(f"[prewarm] clear done file failed market={market}: {e}")

    def _persist_task(self, task: "PrewarmTask") -> None:
        """把单个 task 状态原子写到 <data>/prewarm_status/<market>.json。
        写失败仅 warning，不影响内存进度。
        多 worker 可能并发调用（_process_one 中按 done % 50 触发），tmp 名
        带 uuid 避免互相覆盖；最终 rename 到同一目标，后到者覆盖前者，符合
        "保留最新一次写入"语义。
        """
        d = self._persist_dir()
        if d is None:
            return
        path = d / f"{task.market}.json"
        tmp = d / f"{task.market}.json.tmp.{uuid.uuid4().hex}"
        try:
            data = task.to_dict()
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            # Windows 上 Path.replace 是原子的（同卷），避免半截文件。
            tmp.replace(path)
            task.persist_fail_count = 0  # 成功一次就清零
        except OSError as e:
            task.persist_fail_count += 1
            # 持久化失败本身不影响内存进度（status 接口读内存），但会让进程崩溃后
            # 恢复值偏旧。连续 ≥3 次失败时升级到 ERROR 并写入 task.error_msg，
            # 让前端能看到磁盘异常（典型场景：磁盘满 / 权限改变）。
            if task.persist_fail_count >= 3:
                LogUtil.error(
                    f"[prewarm] persist failing {task.persist_fail_count} times "
                    f"market={task.market}: {e}"
                )
                if not task.error_msg:
                    task.error_msg = (
                        f"持久化失败 {task.persist_fail_count} 次: {e}; "
                        "进程崩溃后恢复进度可能偏旧"
                    )
            else:
                LogUtil.warning(f"[prewarm] persist task failed market={task.market}: {e}")
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass

    # ---------------- 公开 API ----------------

    def start(self, market: str, codes: List[dict]) -> dict:
        """启动一次预热。

        参数 ``codes`` 为 ``[{"code": str, "name": str}, ...]`` 列表。
        返回 ``{"ok": bool, "msg": str, "task": dict | None}``。

        M5 resume：会自动加载 ``<market>_done.txt`` 跳过上次已完成的 code，
        实现进程崩溃 / 取消后续跑；仅在任务 finished 时清空该文件。
        """
        if not codes:
            return {"ok": False, "msg": "标的列表为空，无需预热", "task": None}

        # M1: 速率限制 — 同一 market 在 PREWARM_RATE_LIMIT_SECONDS 内只允许 1 次启动
        if PREWARM_RATE_LIMIT_SECONDS > 0:
            with _prewarm_rate_lock:
                last = _prewarm_last_start_at.get(market, 0.0)
                elapsed = time.time() - last
                if elapsed < PREWARM_RATE_LIMIT_SECONDS:
                    wait_sec = int(PREWARM_RATE_LIMIT_SECONDS - elapsed) + 1
                    return {
                        "ok": False,
                        "code": "rate_limited",
                        "msg": (
                            f"距上次启动 {market!r} 预热不足 "
                            f"{PREWARM_RATE_LIMIT_SECONDS}s，请等待 {wait_sec}s 后再试"
                        ),
                        "task": None,
                    }

        # M5 resume：过滤掉上次已完成的 code（done.txt 里有的）
        done_set = self._load_done_codes(market)
        original_total = len(codes)
        if done_set:
            codes = [c for c in codes if c.get("code") not in done_set]
            skipped = original_total - len(codes)
        else:
            skipped = 0
        if not codes:
            return {
                "ok": False,
                "msg": (
                    f"该市场所有 {original_total} 个标的均已预热完成；"
                    f"如需重新预热，请删除 {market}{_PREWARM_DONE_SUFFIX}"
                ),
                "task": None,
            }

        with self._lock:
            self._gc_old_tasks_locked()
            # L3: 按数据源 group 检查互斥；不同 group 可并行启动
            group = self._market_group(market)
            running_market = self._running_groups.get(group)
            if running_market is not None:
                running_task = self._tasks.get(running_market)
                msg = (
                    f"市场 {running_market!r}（数据源组 {group!r}）已在预热 "
                    f"({running_task.done}/{running_task.total})；"
                    f"同组市场需等待，但其他组的市场（如不同数据源）可并行启动"
                    if running_task
                    else f"组 {group!r} 已有任务在运行，请稍后再试"
                )
                return {
                    "ok": False,
                    "msg": msg,
                    "task": running_task.to_dict() if running_task else None,
                }

            task = PrewarmTask(market=market, total=len(codes))
            task.resumed_skipped = skipped
            self._tasks[market] = task
            self._running_groups[group] = market

        # 注意：worker 线程外部启动，不持锁，避免 worker 内部反向加锁导致死锁。
        thread = threading.Thread(
            target=self._run_task,
            args=(task, codes),
            daemon=True,
            name=f"PrewarmWorker[{market}]",
        )
        self._worker_thread = thread
        thread.start()

        # M1: 仅在确认任务真启动后记录速率限制时间戳；
        # 早期失败（codes 空 / 全已完成 / 已有任务在跑）不计入。
        if PREWARM_RATE_LIMIT_SECONDS > 0:
            with _prewarm_rate_lock:
                _prewarm_last_start_at[market] = time.time()

        LogUtil.info(
            f"[prewarm] task started market={market} total={len(codes)} "
            f"resumed_skipped={skipped} task_id={task.task_id}"
        )
        msg = (
            f"预热任务已启动（resume：跳过上次已完成的 {skipped} 个标的，本次处理 {len(codes)} 个）"
            if skipped
            else "预热任务已启动"
        )
        return {"ok": True, "msg": msg, "task": task.to_dict()}

    def get_status(self, market: str) -> Optional[dict]:
        with self._lock:
            task = self._tasks.get(market)
            return task.to_dict() if task else None

    def cancel(self, market: str) -> dict:
        with self._lock:
            task = self._tasks.get(market)
            if task is None:
                # 这是真正的"找不到资源"，路由侧映射为 404。
                return {"ok": False, "code": "not_found", "msg": "该市场没有预热任务"}
            if task.status != "running":
                # 任务已结束（finished/cancelled/aborted/error）：cancel 是幂等 no-op，
                # 按 200 OK 返回；前端不应把"任务已完成"当作错误处理。
                return {
                    "ok": True,
                    "cancelled": False,
                    "msg": f"任务状态为 {task.status}，无需取消",
                    "task": task.to_dict(),
                }
            task.cancel_event.set()
            return {
                "ok": True,
                "cancelled": True,
                "msg": "已发送取消信号，将在当前批次完成后停止（通常 ≤ 30 秒）",
                "task": task.to_dict(),
            }

    # ---------------- 内部实现 ----------------

    def _gc_old_tasks_locked(self) -> None:
        now = time.time()
        for market in list(self._tasks.keys()):
            t = self._tasks[market]
            if t.status != "running" and t.finished_at and (now - t.finished_at) > PREWARM_TASK_RETAIN_SECONDS:
                self._tasks.pop(market, None)

    def _find_running_task_locked(self) -> Optional[PrewarmTask]:
        """返回当前任意一个仍在运行的 task（多 group 并发时只返回首个）。

        保留此方法供调试/状态汇总用；start() 互斥检查已改为 group 维度，不再依赖此方法。
        """
        for t in self._tasks.values():
            if t.status == "running":
                return t
        return None

    def _run_task(self, task: PrewarmTask, codes: List[dict]) -> None:
        """worker 线程主体：按市场配置的并发度并行处理多个标的。

        关键设计：
        - 多标的并行：用 ThreadPoolExecutor 启 N 个 worker（按市场配置），同时处理多个标的；
        - 单标的内 4 周期再并行：见 ``_prewarm_one_code``；
        - 用户最近看过的标的优先插队：通过维护 pending 队列 + 每轮重新排序；
        - 取消信号：cancel_event 透传到所有子线程，子线程定时检查；
        - 完成统计：用 task._lock_internal 保护 done/succeeded/failed 计数（多线程并发更新）。
        """
        market = task.market
        code_parallelism = PREWARM_CODE_PARALLELISM_BY_MARKET.get(
            market, PREWARM_CODE_PARALLELISM_DEFAULT
        )
        LogUtil.info(
            f"[prewarm] task starting market={market} total={task.total} "
            f"code_parallelism={code_parallelism} freq_parallelism={PREWARM_FREQ_PARALLELISM}"
        )

        # pending 用 list + 索引推进的方式实现"用户最近看过的标的优先插队"
        pending: List[dict] = list(codes)
        processed: set = set()
        # 多线程并发更新 task 计数器需要小锁
        counter_lock = threading.Lock()
        # M5 resume：done 文件路径预先解析一次（None 时降级为不写）
        done_file_path = self._done_file_path(market)

        def _process_one(item: dict) -> None:
            if task.cancel_event.is_set():
                return
            code = item.get("code", "")
            name = item.get("name", "")
            if not code:
                with counter_lock:
                    task.done += 1
                return

            # 当前正在处理（多个 worker 时只展示最新的，无所谓哪一个）；
            # tuple 整体替换避免代码-名称撕裂读。
            task.current = (code, name)

            LogUtil.info(
                f"[prewarm] >>> {market}/{code} ({name}) "
                f"intervals={','.join(PREWARM_INTERVALS)} "
                f"[{task.done + 1}/{task.total}]"
            )

            try:
                cl_config = query_cl_chart_config(market, code)
            except Exception as e:
                LogUtil.error(f"[prewarm] query_cl_chart_config failed {market}/{code}: {e}")
                with counter_lock:
                    task.failed += 1
                    task.done += 1
                return

            try:
                code_ok = self._prewarm_one_code(
                    market=market,
                    code=code,
                    cl_config=cl_config,
                    cancel_event=task.cancel_event,
                )
            except Exception as e:
                LogUtil.error(f"[prewarm] _prewarm_one_code crashed {market}/{code}: {e}")
                code_ok = False

            with counter_lock:
                if code_ok:
                    task.succeeded += 1
                else:
                    task.failed += 1
                task.done += 1
                done_now = task.done

            # M5 resume：把已处理 code（无论成功/失败）追加到 done.txt。
            # 失败也写：避免下次 resume 死循环重试同一个坏 code；
            # 用户判断需要重做时手动删 done.txt 即可。
            # 不上锁：'a' mode + GIL + 单行短写在同进程多线程下足够安全。
            if code and done_file_path is not None:
                try:
                    with open(done_file_path, "a", encoding="utf-8") as fdone:
                        fdone.write(code + "\n")
                except OSError as e:
                    LogUtil.warning(
                        f"[prewarm] append done failed market={market} code={code}: {e}"
                    )

            if done_now % 100 == 0:
                LogUtil.info(
                    f"[prewarm] progress market={market} "
                    f"{done_now}/{task.total} succeeded={task.succeeded} failed={task.failed}"
                )
            # 周期性持久化任务进度（崩溃后最多丢失 _PREWARM_PERSIST_EVERY_N_DONE 个标的的进度）
            if done_now % _PREWARM_PERSIST_EVERY_N_DONE == 0:
                self._persist_task(task)

        # 注册批量预热活动状态：tv.prewarm_common_intervals 看到此标记会让位，
        # 避免逐标的旧版 prewarm 与本任务双倍争抢 chart_calc_locks / 上游 HTTP 配额。
        mark_batch_prewarm_active(market, True)
        try:
            with ThreadPoolExecutor(
                max_workers=code_parallelism,
                thread_name_prefix=f"PrewarmCode[{market}]",
            ) as executor:
                # 分批提交：每次提交 code_parallelism * 4 个任务，避免一次性把 11755 个全塞进去导致
                # 优先级调整失效（已经塞进队列的都是按提交顺序执行）。
                # 每批结束后重新按"用户最近看过"排序剩余 pending。
                batch_size = max(code_parallelism * 4, 8)
                cursor = 0
                # M2: 缓存上一轮的 hot_codes 哈希，hot 列表没变就跳过 O(N) 重排。
                # 典型场景：11k 标的 / 11000 / 8 = 1375 批，用户在跑期间通常只切几次
                # 标的，每次切才需要一次重排；从 O(N²/batch) 降到 ~O(M·N) 实际工作量。
                last_hot_hash = None
                while cursor < len(pending) and not task.cancel_event.is_set():
                    # 优先级调整：把用户最近看过且还没处理的标的提到队首
                    try:
                        hot_codes = _get_user_recent_codes(market) or []
                    except Exception:
                        hot_codes = []
                    cur_hot_hash = tuple(hot_codes) if hot_codes else None
                    if hot_codes and cur_hot_hash != last_hot_hash:
                        pending = self._prioritize_hot_codes(
                            pending, hot_codes, processed, cursor
                        )
                        last_hot_hash = cur_hot_hash

                    end = min(cursor + batch_size, len(pending))
                    batch = pending[cursor:end]
                    cursor = end

                    futures = {}
                    for item in batch:
                        if task.cancel_event.is_set():
                            break
                        c = item.get("code", "")
                        try:
                            fut = executor.submit(_process_one, item)
                        except RuntimeError as e:
                            # executor 已 shutdown 等异常：把当前 item 放回 pending 队首
                            # 不要把 c 加进 processed，否则后续轮次的 hot_codes 重排会跳过它，
                            # 该标的会被静默漏算。
                            LogUtil.warning(
                                f"[prewarm] submit failed market={market} code={c}: {e}"
                            )
                            break
                        futures[fut] = c
                        if c:
                            # 仅在 submit 成功之后才标记为已处理，与 hot_codes 重排逻辑保持一致。
                            processed.add(c)

                    # 等本批完成再调度下一批，确保 hot_codes 重排能生效
                    cancelled_in_batch = False
                    for fut in as_completed(futures):
                        if task.cancel_event.is_set() and not cancelled_in_batch:
                            # 取消时主动让 executor 丢弃尚未启动的 future（Py3.9+），
                            # 否则 with 退出会 join 全部 future，最坏要等 batch_size × 信号量超时。
                            cancelled_in_batch = True
                            executor.shutdown(wait=False, cancel_futures=True)
                            break
                        try:
                            fut.result()
                        except Exception as e:
                            LogUtil.error(
                                f"[prewarm] code worker error {market}/{futures[fut]}: {e}"
                            )

            with self._lock:
                if task.cancel_event.is_set():
                    task.status = "cancelled"
                else:
                    task.status = "finished"
                task.finished_at = time.time()
                task.current = ("", "")
            # M5 resume：仅在 finished（全市场跑完）时清空 done.txt，
            # 让下一次预热重新从头开始；cancel/aborted/error 都保留以便续跑。
            if task.status == "finished":
                self._clear_done_codes(market)

            LogUtil.info(
                f"[prewarm] task done market={market} status={task.status} "
                f"succeeded={task.succeeded} failed={task.failed} "
                f"elapsed={task.finished_at - task.started_at:.1f}s"
            )
        except Exception as e:
            with self._lock:
                # 仅当任务仍处 running 时才覆盖为 error。
                # 若 try 块尾部（如 LogUtil.info 的 finished_at-started_at 格式化）抛异常，
                # 此时 status 已经是 finished/cancelled，不应被倒退为 error。
                if task.status == "running":
                    task.status = "error"
                    task.error_msg = str(e)
                    task.finished_at = time.time()
            LogUtil.error(f"[prewarm] worker crashed market={market}: {e}")
        finally:
            # 终态强制持久化一次（保证崩溃 / 取消都能落盘最后状态）
            # 捕获 _persist_task 可能 leak 出的异常类型：OSError(已被内部吞但保险起见)、
            # TypeError(json.dumps 遇到非 JSON 友好字段)、ValueError(json 编码错误)。
            try:
                self._persist_task(task)
            except (OSError, TypeError, ValueError) as e:
                LogUtil.warning(f"[prewarm] final persist failed: {e}")

            with self._lock:
                # L3: 释放 per-group 互斥；只清除当前 task 占据的 group，
                # 其他 group 的并行 task 不受影响。
                group = self._market_group(market)
                # 防御：仅当当前 group 持有者就是本 task 的 market 时才清除
                # （理论上不会出现错配，但避免异常路径 + 重入导致误清空）
                if self._running_groups.get(group) == market:
                    self._running_groups.pop(group, None)
            # 清除批量预热活动状态：必须在最外层 finally，确保异常路径也释放，
            # 否则 tv.prewarm_common_intervals 会被永久误判为"批量预热中"而不工作。
            mark_batch_prewarm_active(market, False)

    @staticmethod
    def _yield_to_user_if_active(cancel_event: threading.Event) -> None:
        """如果用户最近活跃（有 firstDataRequest=true 的请求），sleep 让出 QPS。

        关键逻辑：
        - 不会无限等待（最多让 N 次，每次 PREWARM_YIELD_SLEEP_SECONDS）；
        - 每次 sleep 后重新检查用户活跃度，用户彻底闲下来就立刻继续；
        - 检查 cancel_event 以快速响应取消。

        为什么不用 chart_calc_locks 自然处理：
        - chart_calc_locks 是 per-cache_key 的，只防止同一标的同周期重复算；
        - 但数据源的 QPS 限制是**全局的**——预热打 GDS 的 1D，会跟用户打 ZK 的 5min 抢 QPS。
        - 所以必须用一个全局信号量 + 用户活跃度感知来彻底分隔。
        """
        max_yields = 5  # 最多让 5 次（5 秒），避免预热被永久饿死
        for _ in range(max_yields):
            if cancel_event.is_set():
                return
            try:
                last_user_req = _get_last_user_request_time()
            except Exception:
                return
            idle = time.time() - last_user_req
            if idle >= PREWARM_USER_ACTIVE_WINDOW_SECONDS:
                return
            time.sleep(PREWARM_YIELD_SLEEP_SECONDS)

    @staticmethod
    def _prioritize_hot_codes(
        pending: List[dict],
        hot_codes: List[str],
        processed: set,
        cursor: int = 0,
    ) -> List[dict]:
        """把 pending[cursor:] 中属于 hot_codes 且未处理的项移到 cursor 位置（队首）。

        - 已经处理过（processed）或已被前面 batch 提交（< cursor）的不动；
        - 保持 hot_codes 内部的顺序（最近的在最前）；
        - cursor 之前的部分（已经提交给 executor）保持不变。
        """
        if not hot_codes or cursor >= len(pending):
            return pending
        hot_set = set(c for c in hot_codes if c not in processed)
        if not hot_set:
            return pending

        head = pending[:cursor]
        tail = pending[cursor:]

        front = []
        rest = []
        code_to_item = {}
        for item in tail:
            c = item.get("code", "")
            if c in hot_set:
                code_to_item[c] = item
            else:
                rest.append(item)
        for c in hot_codes:
            if c in code_to_item:
                front.append(code_to_item[c])
        return head + front + rest

    def _prewarm_one_code(
        self,
        market: str,
        code: str,
        cl_config: dict,
        cancel_event: threading.Event,
    ) -> bool:
        """预热单个标的的 4 个常用周期，**4 个周期并行计算**。返回是否至少有 1 个成功。

        关键设计变化（2026-04 重构）：
        - 旧实现：4 个周期串行，单标的总耗时 = sum(各周期)，~30s/标的；
        - 新实现：4 个周期用 ThreadPoolExecutor 并发，单标的总耗时 = max(各周期)，~10s/标的。

        为什么可以并行：
        - 每个周期独立拉数据（ex.klines）→ 独立算缠论 → 独立写 cache_key 不同的缓存；
        - 4 个周期之间没有数据依赖（higher_macd 是从当前周期 closes 算的，不依赖其他周期）；
        - cache_lock 是写缓存的细粒度锁，多个 cache_key 同时写不会冲突；
        - chart_calc_locks 是 per-cache_key 锁，确保即使用户切到该标的同一周期，
          tv_history 和这里的预热也只会有一个在算（另一个等结果）。

        移除了 _wait_for_user_idle：让 chart_calc_locks 自然处理"用户切到正在预热的标的"
        的情况，不再阻塞 worker。
        """
        any_success = False
        success_lock = threading.Lock()

        def _compute_one_freq(interval: str) -> bool:
            if cancel_event.is_set():
                return False
            freq = resolution_maps.get(interval, interval)
            cache_key = _build_cache_key(market, code, freq, cl_config)

            # 已在缓存里就跳过（用户刚看过 / 上一轮预热刚算完）
            with cache_lock:
                if cache_key in chart_data_cache:
                    return True

            # 2026-04 新增：磁盘冷层命中也算预热完成。
            # 进程重启或 RAM TTL 淘汰后，磁盘里仍有上次预热的结果——直接 warm 回 RAM
            # 而不重算，可省下整次 ex.klines + 缠论计算 + MACD 的开销。
            # _get_chart_cache_entry 内部已带磁盘 fallback + RAM 回填。
            disk_entry = _get_chart_cache_entry(cache_key)
            if disk_entry is not None and disk_entry.get("is_full_snapshot"):
                return True

            # 让位：用户最近 N 秒内有主动请求 → 预热等一下，把数据源的 QPS 让给用户
            self._yield_to_user_if_active(cancel_event)
            if cancel_event.is_set():
                return False

            # 全局信号量：限制同一时刻有多少个预热请求在打数据源
            # 用户的实时 tv_history 请求不受这个信号量限制，永远优先
            # 用 1s 轮询而非一次 30s acquire：让 cancel_event 能在 ≤1s 内打断等待，
            # 避免取消信号要等 30s 信号量超时才生效。
            acquired = False
            deadline = time.time() + 30.0
            while not acquired:
                if cancel_event.is_set():
                    return False
                remaining = deadline - time.time()
                if remaining <= 0:
                    LogUtil.warning(
                        f"[prewarm] semaphore timeout, skip {market}/{code}/{interval}"
                    )
                    return False
                acquired = _PREWARM_INFLIGHT_SEMAPHORE.acquire(timeout=min(1.0, remaining))

            try:
                # M4: 1 次 2s 退避重试覆盖数据源短暂抖动（HTTP 超时/连接重置等）。
                # 不做更多次重试，避免数据源真宕机时把信号量占住挤掉用户实时请求。
                # cancel-aware：重试前检查 cancel_event，避免取消后还跑一遍。
                last_err: Optional[BaseException] = None
                for attempt in range(2):
                    try:
                        if compute_and_cache_chart_data(market, code, freq, cl_config):
                            if attempt > 0:
                                LogUtil.info(
                                    f"[prewarm] compute retry succeeded "
                                    f"{market}/{code}/{interval}"
                                )
                            return True
                        last_err = RuntimeError("compute returned False")
                    except Exception as e:
                        last_err = e
                    if attempt == 0 and not cancel_event.is_set():
                        time.sleep(2.0)
                LogUtil.error(
                    f"[prewarm] compute failed after retry "
                    f"{market}/{code} interval={interval}: {last_err}"
                )
                return False
            finally:
                _PREWARM_INFLIGHT_SEMAPHORE.release()

        # 单标的内 4 周期并行
        with ThreadPoolExecutor(
            max_workers=PREWARM_FREQ_PARALLELISM,
            thread_name_prefix=f"PrewarmFreq[{market}/{code}]",
        ) as freq_executor:
            future_to_interval = {
                freq_executor.submit(_compute_one_freq, interval): interval
                for interval in PREWARM_INTERVALS
            }
            for fut in as_completed(future_to_interval):
                if cancel_event.is_set():
                    break
                try:
                    if fut.result():
                        with success_lock:
                            any_success = True
                except Exception as e:
                    interval = future_to_interval[fut]
                    LogUtil.error(
                        f"[prewarm] freq worker error {market}/{code}/{interval}: {e}"
                    )

        return any_success

# 单例
_prewarm_manager = PrewarmManager()

@symbols_bp.route("/symbols/prewarm", methods=["POST"])
@login_required
def symbols_prewarm():
    """启动当前市场的全量缠论数据预热。

    Body 参数（JSON 或 form）：
    - market : 市场代码（必填）
    """
    market = (request.values.get("market") or "").strip().lower()
    if not market:
        # 兼容 JSON body
        body = request.get_json(silent=True) or {}
        market = (body.get("market") or "").strip().lower()
    if market not in market_types:
        return jsonify({"ok": False, "msg": f"未知市场: {market!r}"}), 400

    try:
        all_stocks = get_cached_processed_stocks(market, allow_sync_fallback=True) or []
    except Exception as e:
        LogUtil.error(f"[symbols_prewarm] get stocks failed market={market}: {e}")
        return jsonify({"ok": False, "msg": f"获取标的列表失败: {e}"}), 500

    # 与展示列表过滤同步: 不预热被列表过滤掉的 ETF/Fund/非个股.
    all_stocks = _apply_market_filter(market, all_stocks)

    codes = [
        {"code": s.get("code", ""), "name": s.get("name", "")}
        for s in all_stocks
        if s.get("code")
    ]

    result = _prewarm_manager.start(market, codes)
    if result["ok"]:
        status_code = 200
    elif result.get("code") == "rate_limited":
        status_code = 429  # Too Many Requests
    else:
        status_code = 409
    return jsonify(result), status_code

@symbols_bp.route("/symbols/prewarm/status")
@login_required
def symbols_prewarm_status():
    """查询某市场最近一次预热任务的进度。"""
    market = (request.args.get("market") or "").strip().lower()
    if market not in market_types:
        return jsonify({"ok": False, "msg": f"未知市场: {market!r}"}), 400

    task = _prewarm_manager.get_status(market)
    if task is None:
        return jsonify({"ok": True, "task": None})
    return jsonify({"ok": True, "task": task})

@symbols_bp.route("/symbols/prewarm/cancel", methods=["POST"])
@login_required
def symbols_prewarm_cancel():
    """取消某市场正在运行的预热任务。"""
    market = (request.values.get("market") or "").strip().lower()
    if not market:
        body = request.get_json(silent=True) or {}
        market = (body.get("market") or "").strip().lower()
    if market not in market_types:
        return jsonify({"ok": False, "msg": f"未知市场: {market!r}"}), 400

    result = _prewarm_manager.cancel(market)
    if result["ok"]:
        status_code = 200
    else:
        # 当前唯一 ok=False 的分支是 "not_found"；其他状态走 200 幂等返回。
        status_code = 404 if result.get("code") == "not_found" else 409
    return jsonify(result), status_code