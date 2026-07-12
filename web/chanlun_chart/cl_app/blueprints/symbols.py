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

import hashlib
import json
import os
import pathlib
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple


# env 优先的常量读取助手；解析失败兜底默认值，不让坏 env 炸服务启动。
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

from chanlun import config
from chanlun.cl_utils import query_cl_chart_config
from chanlun.tools.log_util import LogUtil

from ..services.chart_cache import (
    _build_cache_key,
    _get_chart_cache_entry,
    cache_lock,
    chart_data_cache,
)
from ..services.chart_compute import compute_and_cache_chart_data, market_now_trading
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

# 缓存 ``_apply_market_filter`` 的结果 (TTL 60s), 避免每次请求重跑 11k 条正则/type 检查。
# fingerprint 基于 items 数量 + 首/中/尾 code 的 12-char md5，配合 ETag 304 短路重复请求。
_market_filtered_cache: Dict[str, Tuple[List[dict], str, float]] = {}
_market_filtered_lock = threading.Lock()
_MARKET_FILTERED_TTL = 60.0


def _items_fingerprint(items: List[dict]) -> str:
    """快速 fingerprint: 不对 11k 条做完整 md5, 取 count + 首/中/尾 code."""
    if not items:
        return "0"
    n = len(items)
    parts = [
        str(n),
        items[0].get("code", ""),
        items[n // 2].get("code", ""),
        items[-1].get("code", ""),
    ]
    return hashlib.md5("|".join(parts).encode()).hexdigest()[:12]


def _get_market_filtered_stocks(market: str) -> Tuple[List[dict], str]:
    """带 TTL 缓存的 (filtered_list, fingerprint).

    cache miss / expired 时:
    1. 调 ``get_cached_processed_stocks`` 取原始列表
    2. 跑 ``_apply_market_filter`` (a 股 type 过滤 + us 黑名单正则)
    3. 计算 fingerprint, 写回缓存
    """
    now = time.time()
    with _market_filtered_lock:
        cached = _market_filtered_cache.get(market)
        if cached and now - cached[2] < _MARKET_FILTERED_TTL:
            return cached[0], cached[1]

    try:
        raw = get_cached_processed_stocks(market, allow_sync_fallback=True) or []
    except Exception as e:
        LogUtil.error(f"[symbols_list] get stocks failed market={market}: {e}")
        raw = []
    filtered = _apply_market_filter(market, raw)
    fp = _items_fingerprint(filtered)
    with _market_filtered_lock:
        _market_filtered_cache[market] = (filtered, fp, now)
    return filtered, fp


def _build_response_etag(
    market_fp: str, q: str, page: int, page_size: int, return_all: bool
) -> str:
    """ETag: 市场数据 fingerprint + 请求参数，任一变化 ETag 即变。

    用 weak ETag (W/) 是因 fingerprint 不是字节精确的内容 hash，仅近似。
    """
    raw = f"{market_fp}|{q}|{page}|{page_size}|{int(return_all)}"
    return f'W/"{hashlib.md5(raw.encode()).hexdigest()[:16]}"'

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


def _apply_market_filter(
    market: str, all_stocks: List[dict], for_prewarm: bool = False
) -> List[dict]:
    """对标的列表应用市场过滤(type 白名单 + 美股 name 黑名单).

    供 symbols_list (展示) 和 symbols_prewarm (预热) 共用, 确保
    预热范围与用户能搜到的标的一致, 不浪费 CPU/磁盘算 ETF 等.

    ``for_prewarm=True`` 时启用仅供预热路径的额外过滤
    (如 US 市场 ``US_PREWARM_ZIXUAN_ONLY`` 的自选股交集),
    展示路径必须保持默认 False, 否则用户看到的列表会被错误地
    截断为自选股 (历史 bug: prewarm 自选股交集错误地溢出到展示路径).
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
    # US 市场 prewarm 仅限自选股，保护长桥月度配额。
    # query_all_zs_stocks 返回分组嵌套结构，需从 stocks 子列表抽 code 拍扁。
    if for_prewarm and market == "us" and getattr(config, "US_PREWARM_ZIXUAN_ONLY", False):
        from chanlun.zixuan import ZiXuan
        zx_codes = {
            stock["code"]
            for group in ZiXuan("us").query_all_zs_stocks()
            for stock in group.get("stocks", [])
        }
        before = len(all_stocks)
        all_stocks = [s for s in all_stocks if s.get("code") in zx_codes]
        LogUtil.info(
            f"[_apply_market_filter] US prewarm 仅限自选股，{before} → {len(all_stocks)}"
        )
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

    # 带 TTL 缓存 + ETag，避免每次重跑 _apply_market_filter 11k 条正则
    all_stocks, market_fp = _get_market_filtered_stocks(market)

    # ETag 检查在过滤/分页之前：同一参数组合命中即 304，服务端跳过 query 扫描和切片
    etag = _build_response_etag(market_fp, query, page, page_size, return_all)
    if request.headers.get("If-None-Match") == etag:
        resp = ("", 304)
        return resp

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

    resp = jsonify(
        {
            "ok": True,
            "market": market,
            "total": total,
            "page": page,
            "page_size": page_size,
            "items": items,
        }
    )
    resp.headers["ETag"] = etag
    # private: 含用户特定数据 (虽然 symbols 公开, 走 login_required 不应被 CDN 缓存)
    # max-age=600: 与 _MARKET_FILTERED_TTL 60s 协同, 浏览器最多 10min 重发
    resp.headers["Cache-Control"] = "private, max-age=600"
    return resp

# ---------------------------------------------------------------------------
# 缠论数据预热（Pre-warm）
# ---------------------------------------------------------------------------

# 预热使用的常用周期（TV interval 表示法），通过 resolution_maps 转成项目内部 freq：
# "1D" -> d, "30" -> 30m, "5" -> 5m, "1" -> 1m
# 默认只预热 30m/5m/1m(去掉日线): 减少每只标的的取数+计算量,加速全量预热;日线首次看时
# 按需算(~1.3s,之后缓存秒开)。env PREWARM_INTERVALS 逗号分隔可调,例如:
#   "5,1"      仅 1m+5m(更快,~再减一档)
#   "1D,30,5,1" 恢复全部 4 周期
PREWARM_INTERVALS = [
    s.strip() for s in os.environ.get("PREWARM_INTERVALS", "30,5,1").split(",") if s.strip()
]

# 单标的内并发计算的周期数（每个周期一个线程）。
# 实测全市场预热同时打 8 个并发请求时（2 标的 × 4 周期），长桥 HTTP 连接池被占满，
# 前端 polling 请求被排到队列后面，导致切换标的耗时 10-18 秒。
# 维持 2，让总在飞请求数 ≤ INFLIGHT_LIMIT，避免 4 周期同时抢信号量。
# env PREWARM_FREQ_PARALLELISM 可覆盖
PREWARM_FREQ_PARALLELISM = _env_int("PREWARM_FREQ_PARALLELISM", 2)

# QMT 批量预下载加速: A股预热前先用 download_history_data2 批量拉基础周期(1m/5m/1d)数据到
# 本地库, 之后逐只计算传 skip_download=True 跳过逐只 download —— 把成千上万次 QMT 往返合并成
# 少量批量调用。env PREWARM_QMT_BATCH=0 可一键关闭回退到逐只 download(若批量下载异常)。
# env PREWARM_QMT_BATCH_CHUNK 控制单批标的数(默认 100, 调小更稳防 BSON payload 过大)。
PREWARM_QMT_BATCH = _env_int("PREWARM_QMT_BATCH", 0)
PREWARM_QMT_BATCH_CHUNK = _env_int("PREWARM_QMT_BATCH_CHUNK", 100)

# native 数据源（xtquant / tdx 系列）客户端线程不安全，单标的内多周期计算必须串行。
# 按 config.EXCHANGE_<MARKET> 实际数据源归类：a→qmt(xtquant)、futures→tdx_futures、
# ny_futures→tdx_ny_futures、fx→tdx_fx 均为 native；hk/us→cq(长桥 HTTP)、
# currency/currency_spot→binance(HTTP) 可并行。
# 注：不沿用 tv.py 的 _PREWARM_INTERVALS_PARALLEL_MARKETS——那份集合把 tdx 的 fx
# 误列为可并行，是既有缺陷。
_NATIVE_SERIAL_MARKETS = frozenset({"a", "futures", "ny_futures", "fx"})


def _resolve_freq_parallelism(market: str) -> int:
    """返回单标的内"周期并行度"。

    native 市场（a 股 xtquant、tdx 期货）→ 1（强制串行，客户端线程不安全）；
    其余 HTTP 数据源市场 → ``PREWARM_FREQ_PARALLELISM``。

    历史 bug：旧实现对所有市场统一用 PREWARM_FREQ_PARALLELISM，a 股批量预热
    会并发 2 个 xtquant ``ex.klines`` 调用，与"native 必须串行"约束冲突。
    """
    if market in _NATIVE_SERIAL_MARKETS:
        return 1
    return PREWARM_FREQ_PARALLELISM


# 多个标的之间并行处理的 worker 数。按市场区分：
# - a 股 xtquant native 不是线程安全的，必须串行 → 1
# - 其他 native 数据源（tdx 期货）也保险串行 → 1
# - HTTP 数据源（长桥/futu）放开并行：用户切到已预热标的命中 disk 毫秒级返回，
#   预热可以更激进抢占数据源。总在飞受 INFLIGHT_LIMIT 全局信号量约束，不会无限叠加。
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

# 全局在飞请求数信号量上限（进程内，不跨进程；本服务单进程部署假设成立）。
# 长桥/futu 单连接池 QPS 上限实测 ~10，预热占 6，给用户实时请求和 polling 留 4 个余量。
# 用户的 tv_history 不走此信号量，永远优先；用户切已预热标的走 disk hit，不抢 HTTP 配额。
# env PREWARM_GLOBAL_INFLIGHT_LIMIT 可覆盖
PREWARM_GLOBAL_INFLIGHT_LIMIT = _env_int("PREWARM_GLOBAL_INFLIGHT_LIMIT", 6)

# 用户最近 N 秒内有 firstDataRequest=true 的请求时，预热等一下再发，
# 避免把用户实时请求挤到 HTTP 连接队列后面。env PREWARM_USER_ACTIVE_WINDOW_SECONDS 可覆盖
PREWARM_USER_ACTIVE_WINDOW_SECONDS = _env_float("PREWARM_USER_ACTIVE_WINDOW_SECONDS", 3.0)
# 让位等待时间（秒）。0.3s 既能让用户突发请求优先，又不会因单次 first=true 把预热堵几秒。
# env PREWARM_YIELD_SLEEP_SECONDS 可覆盖
PREWARM_YIELD_SLEEP_SECONDS = _env_float("PREWARM_YIELD_SLEEP_SECONDS", 0.3)

# 任务对象保留时长：完成后超过此时间允许新任务启动，并允许 GC。
PREWARM_TASK_RETAIN_SECONDS = 3600
# 用户最近请求过的标的优先插队：worker 在每轮循环开始时，会把还没预热的"用户最近看过"
# 的标的提到队首。

# 任务进度持久化目录与写盘频率
_PREWARM_PERSIST_DIRNAME = "prewarm_status"
# 写盘频率：每完成 N 个标的写一次（终态时强制写一次）。
# 50 是经验值：11k 标的约 220 次写盘，IO 开销 < 1%，崩溃后丢失进度 < 50 个。
# max(1, ...) 防 env 写 0 触发 done_now % 0 的 ZeroDivisionError。env PREWARM_PERSIST_EVERY_N_DONE 可覆盖。
_PREWARM_PERSIST_EVERY_N_DONE = max(1, _env_int("PREWARM_PERSIST_EVERY_N_DONE", 50))
# Resume 用的"已完成 code 列表"文件后缀：
# - 每完成一个 code（无论成功/失败）即追加一行，进程崩溃/取消后下次 start() 跳过；
# - 仅在任务 status==finished（全市场跑完）时删除，cancel/aborted/error 都保留以便续跑；
# - 文件格式为 plain text，每行一个 code，依赖 'a' mode + GIL 实现单进程多线程的写并发。
_PREWARM_DONE_SUFFIX = "_done.txt"

# 全局信号量：所有预热请求（不论标的不论周期）都要先 acquire 才能发请求。
# 这是防止打爆数据源的核心机制。
_PREWARM_INFLIGHT_SEMAPHORE = threading.Semaphore(PREWARM_GLOBAL_INFLIGHT_LIMIT)

# 启动速率限制：同一 market 在 N 秒内只允许成功启动 1 次预热，
# 防止反复 POST 滥用 CPU/磁盘/数据源 QPS。设为 0 禁用。env PREWARM_RATE_LIMIT_SECONDS 可覆盖。
PREWARM_RATE_LIMIT_SECONDS = _env_int("PREWARM_RATE_LIMIT_SECONDS", 300)
# market -> last successful start ts；进程内字典，单 worker 假设下足够。
_prewarm_last_start_at: Dict[str, float] = {}
_prewarm_rate_lock = threading.Lock()

# done.txt append 写锁。Windows + NTFS 上 'a' mode 不保证原子，多 worker
# 并发 append 可能粘连产生坏行；用模块级写锁串行化所有 append，开销可忽略。
_done_file_write_lock = threading.Lock()

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
        # 本任务因续跑而跳过的 code 数（仅展示用，total 已扣除）
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
        # per-group 互斥状态（group → 当前在跑的 market）。
        # 同一 group 内严格互斥；不同 group 可并发。
        self._running_groups: Dict[str, str] = {}
        # worker 线程引用（仅用于调试，不主动 join）
        self._worker_thread: Optional[threading.Thread] = None
        # 历史任务文件惰性加载——避免 Flask 启动时同步扫盘阻塞，
        # 数据目录暂时不可达时进程仍能起来；首次 start/get_status/cancel 时再触发加载。
        self._loaded: bool = False
        self._load_lock = threading.Lock()  # 独立锁避免与 self._lock 嵌套
        # _persist_task 序列化锁——避免多 worker 并发持久化时后到者用旧 snapshot 倒退 done 数。
        # to_dict() + write + replace 整体放进 lock 内，保证 latest-wins。
        self._persist_lock = threading.Lock()

    # ---------------- 数据源分组 ----------------

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

    def _ensure_loaded(self) -> None:
        """首次调用时载入历史任务（惰性加载）。

        double-checked locking 防止重复 IO；快路径无锁直接返回。
        所有 public method（start/get_status/cancel）入口处调用此方法。
        """
        if self._loaded:
            return
        with self._load_lock:
            if self._loaded:
                return
            self._load_persisted_tasks()
            self._loaded = True

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

    # ---------------- 已完成 code 列表（续跑支持） ----------------

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

        写失败仅 warning，不影响内存进度。tmp 名带 uuid 避免并发覆盖；
        最终 rename 到同一目标，后到者覆盖前者，符合 latest-wins 语义。
        """
        d = self._persist_dir()
        if d is None:
            return
        path = d / f"{task.market}.json"
        tmp = d / f"{task.market}.json.tmp.{uuid.uuid4().hex}"
        # 整体串行化 snapshot+write+replace：锁内 to_dict 保证每次写盘都用最新 done 值，
        # 防止并发时后到的 replace 把磁盘 done 倒退（latest-wins）。
        try:
            with self._persist_lock:
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

        自动加载 ``<market>_done.txt`` 跳过上次已完成的 code，实现续跑；
        仅在任务 finished（全市场跑完）时清空该文件。
        """
        if not codes:
            return {"ok": False, "msg": "标的列表为空，无需预热", "task": None}

        # 惰性加载历史任务文件
        self._ensure_loaded()

        # 速率限制：同一 market 在 PREWARM_RATE_LIMIT_SECONDS 内只允许 1 次启动
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

        # 过滤掉上次已完成的 code（done.txt 里有的），实现续跑
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
            # 按数据源 group 检查互斥；不同 group 可并行启动
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

        # 仅在确认任务真启动后记录速率限制时间戳；
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
        self._ensure_loaded()
        with self._lock:
            task = self._tasks.get(market)
            return task.to_dict() if task else None

    def cancel(self, market: str) -> dict:
        self._ensure_loaded()
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
            f"code_parallelism={code_parallelism} "
            f"freq_parallelism={_resolve_freq_parallelism(market)}"
        )

        # pending 用 list + 索引推进的方式实现"用户最近看过的标的优先插队"
        pending: List[dict] = list(codes)
        processed: set = set()
        # 多线程并发更新 task 计数器需要小锁
        counter_lock = threading.Lock()
        # done 文件路径预先解析一次（None 时降级为不写）
        done_file_path = self._done_file_path(market)

        # 批量预下载是否成功(QMT 专属);成功后逐只 compute 跳过 download。_process_one 闭包读取,
        # 在下方批量下载完成后被重新赋值(读取发生在 executor 调度之后, 取到的是最终值)。
        batch_predownloaded = False

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
                    skip_download=batch_predownloaded,
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

            # 把已处理 code（无论成功/失败）追加到 done.txt 以支持续跑。
            # 失败也写：避免下次续跑死循环重试坏 code；取消不写，确保未完成标的下次重试。
            # Windows + NTFS 上 'a' mode 不原子，用模块级写锁串行 append。
            if code and done_file_path is not None and not task.cancel_event.is_set():
                try:
                    with _done_file_write_lock:
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

        # QMT 批量预下载(加速): 逐只计算前先把所有标的的基础周期数据批量拉到本地库,
        # 之后逐只 compute 传 skip_download=True 跳过逐只 download。仅 A股/QMT(交易所暴露
        # prewarm_batch_download)生效; 任何异常都降级(batch_predownloaded=False, 逐只仍 download)。
        # 交易时段跳过批量预下载: prewarm_batch_download 同步持 _XTDATA_NATIVE_LOCK(与用户
        # 实时 klines() 同锁),盘中触发会长时间卡住所有看盘/SSE 刷新的用户。预热本就该盘前/盘后
        # 热身,交易时段直接跳过、保持 batch_predownloaded=False 走原有逐只 download 路径
        # (不影响正确性,仅无批量加速)。
        if PREWARM_QMT_BATCH and market_now_trading(market):
            LogUtil.info(
                f"[prewarm] 交易时段跳过批量预下载 market={market}, 走逐只 download"
            )
        elif PREWARM_QMT_BATCH:
            try:
                from chanlun.exchange import get_exchange
                from chanlun.market import Market
                _ex = get_exchange(Market(market))
                if hasattr(_ex, "prewarm_batch_download"):
                    _freqs = [resolution_maps.get(iv, iv) for iv in PREWARM_INTERVALS]
                    _t0 = time.time()
                    LogUtil.info(
                        f"[prewarm] 批量预下载开始 market={market} 标的={len(codes)} "
                        f"周期={_freqs} chunk={PREWARM_QMT_BATCH_CHUNK}"
                    )
                    _ex.prewarm_batch_download(
                        [c.get("code", "") for c in codes if c.get("code")],
                        _freqs,
                        cancel_check=lambda: task.cancel_event.is_set(),
                        progress_callback=lambda base, done, total: setattr(
                            task, "current", ("批量预下载", f"{base} {done}/{total}")
                        ),
                        chunk_size=PREWARM_QMT_BATCH_CHUNK,
                    )
                    batch_predownloaded = not task.cancel_event.is_set()
                    LogUtil.info(
                        f"[prewarm] 批量预下载完成 market={market} "
                        f"耗时={time.time() - _t0:.1f}s skip_download={batch_predownloaded}"
                    )
            except Exception as e:
                LogUtil.warning(f"[prewarm] 批量预下载失败, 降级逐只 download: {e}")
                batch_predownloaded = False

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
                # 缓存上一轮的 hot_codes 哈希，hot 列表没变就跳过 O(N) 重排。
                # 用户切标的时才触发重排，避免每批都做 O(N) 扫描。
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
                            # executor 已 shutdown(异常态)无法再 submit。executor 是 per-task 单例
                            # (with 在 while 外),一旦死透不可恢复:原 break 只跳出本批 for,外层 while
                            # 会逐批 RuntimeError→break 把剩余标的静默漏算、进度永卡在 total 之下;rewind
                            # cursor 重试则因 executor 仍死而死循环(审查 B-2)。故直接抛出,让外层 try 把
                            # 任务标 error(可见失败),不静默卡死、不死循环。
                            LogUtil.error(
                                f"[prewarm] executor submit failed, abort task market={market} code={c}: {e}"
                            )
                            raise
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
            # 仅在 finished（全市场跑完）时清空 done.txt，让下次从头开始；
            # cancel/aborted/error 都保留，支持续跑。
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
                # 释放 per-group 互斥；只清除本 task 占据的 group，其他 group 不受影响。
                # 防御：仅当 group 持有者确为本 market 时才清除，避免异常路径误清空。
                group = self._market_group(market)
                if self._running_groups.get(group) == market:
                    self._running_groups.pop(group, None)
            # 清除批量预热活动状态：必须在最外层 finally，确保异常路径也释放，
            # 否则 prewarm_common_intervals 会被永久误判为"批量预热中"而不工作。
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
        skip_download: bool = False,
    ) -> bool:
        """预热单个标的的 4 个常用周期，**4 个周期并行计算**。返回是否至少有 1 个成功。

        关键设计变化（2026-04 重构）：
        - 旧实现：4 个周期串行，单标的总耗时 = sum(各周期)，~30s/标的；
        - 新实现：4 个周期用 ThreadPoolExecutor 并发，单标的总耗时 = max(各周期)，~10s/标的。

        为什么可以并行：
        - 每个周期独立拉数据（ex.klines）→ 独立算缠论 → 独立写 cache_key 不同的缓存；
        - 4 个周期之间没有数据依赖（higher_macd 是从当前周期 closes 算的，不依赖其他周期）；
        - cache_lock 是写缓存的细粒度锁，多个 cache_key 同时写不会冲突。

        注意（M5 → R8-C2 修正）：compute_and_cache_chart_data 现以非阻塞方式获取
        chart_calc_locks（与 tv_history/_do_revalidate 同锁域）。用户正持锁计算同一
        cache_key 时预热让位跳过（返回 True，用户结果会入缓存），既去重复计算、又消除
        预热读共享 CL 与用户 path-2 增量改写的并发撕裂（原 M5 "cache_lock 保正确性" 仅护
        dict 写入，漏了共享 CL 的并发读/改）。
        单标的内的周期并行度由 _resolve_freq_parallelism(market) 决定：native
        市场（a/futures/ny_futures）强制串行 1。
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

            # 磁盘冷层命中也算预热完成：进程重启或 RAM TTL 淘汰后磁盘仍有旧结果，
            # 直接 warm 回 RAM 而不重算，省下 ex.klines + 缠论计算 + MACD 的开销。
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
                # 1 次 2s 退避重试覆盖数据源短暂抖动（HTTP 超时/连接重置等）。
                # 不做更多次重试，避免数据源真宕机时把信号量占住挤掉用户实时请求。
                # 重试前检查 cancel_event，避免取消后还跑一遍。
                last_err: Optional[BaseException] = None
                for attempt in range(2):
                    # 第二次重试强制真正 download: 批量某 chunk 失败时,受影响标的本地库可能缺数据,
                    # 首次 skip_download=True 会读到空/不完整数据而失败;兜底让 attempt==1 老老实实
                    # 单独下载,不被批量加速的 skip_download 卡死。attempt==0 仍遵循外层入参。
                    attempt_skip = skip_download if attempt == 0 else False
                    try:
                        if compute_and_cache_chart_data(market, code, freq, cl_config, skip_download=attempt_skip):
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
            max_workers=_resolve_freq_parallelism(market),
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
        body = request.get_json(silent=True)
        if request.is_json and not isinstance(body, dict):
            return jsonify(
                {"ok": False, "msg": "JSON body must be an object."}
            ), 400
        body = body or {}
        market = (body.get("market") or "").strip().lower()
    if market not in market_types:
        return jsonify({"ok": False, "msg": f"未知市场: {market!r}"}), 400

    try:
        all_stocks = get_cached_processed_stocks(market, allow_sync_fallback=True) or []
    except Exception as e:
        LogUtil.error(f"[symbols_prewarm] get stocks failed market={market}: {e}")
        return jsonify({"ok": False, "msg": f"获取标的列表失败: {e}"}), 500

    # 与展示列表过滤同步: 不预热被列表过滤掉的 ETF/Fund/非个股.
    # for_prewarm=True 启用 US 市场仅自选股的额外过滤(保护长桥月度配额),
    # 展示路径不能开此开关, 否则会把用户能看到的列表截断为自选股.
    all_stocks = _apply_market_filter(market, all_stocks, for_prewarm=True)

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
        body = request.get_json(silent=True)
        if request.is_json and not isinstance(body, dict):
            return jsonify(
                {"ok": False, "msg": "JSON body must be an object."}
            ), 400
        body = body or {}
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
