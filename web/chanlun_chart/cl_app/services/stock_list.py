"""标的列表服务（service）。

设计要点：
- 启动后延迟 ``PRELOAD_STARTUP_DELAY_SECONDS`` 才开始第一轮；若已从磁盘恢复缓存，
  首轮只确认缓存可用，不立即访问交易所，避免抢占首屏行情请求所需资源。
- 之后每 ``PRELOAD_INTERVAL_SECONDS`` 周期性刷新同一显式准入身份集合。
- 单市场预加载失败/超时不会影响其他市场（_preload_single_exchange 自吞异常）。
- 缓存 miss 时按调用方意愿走两条路径：
  - allow_sync_fallback=False: 触发异步刷新 + raise（适合不能阻塞的初始化路径）
  - allow_sync_fallback=True: 同步调一次（适合用户搜索，宁可慢也不能 500）

API：
- ``stock_cache``: LRUCache(100)，市场 → 最后一次成功处理的 symbols 列表
- ``preload_symbols`` / ``start_symbol_preload_thread``: 后台预加载入口
- ``get_cached_processed_stocks(exchange, allow_sync_fallback=False)``: 列表读取入口
- ``get_cached_processed_stock(exchange, code)``: 无 I/O 的单标的读取入口
- ``_trigger_async_refresh(exchange)``: 单例化的异步刷新触发器
"""
import json
import os
import re
import tempfile
import threading
import time
from collections.abc import Mapping

from cachetools import LRUCache
import pinyin

from chanlun import config
from chanlun.market import Market
from chanlun.exchange import get_exchange, resolve_bounded_stock_info
from chanlun.tools.log_util import LogUtil
from .trading_screening_scope import (
    DEFAULT_VALIDATION_COHORT_SIZE,
    admit_explicit_validation_codes,
)

# 基础数据缓存（市场 → processed symbols list）
# 刷新失败后仍须保留最后一次已知有效值；刷新节奏用于跟踪新鲜度。
stock_cache: LRUCache = LRUCache(maxsize=100)
# stock_cache 使用专用锁；过去与 chart_cache.cache_lock 共用锁时会导致一次
# chart 缓存的磁盘读会阻塞 symbol 列表读写；此处独立成锁，两个无关缓存互不干扰。
_stock_cache_lock = threading.Lock()

# 落盘缓存只接受这一份当前契约；不维护历史格式或版本分支。
_STOCKS_CACHE_SCHEMA = "chanlun-stock-list-cache"
_A_STOCK_CODE = re.compile(r"^(?:SH|SZ|BJ)\.\d{6}$")
_KNOWN_A_INSTRUMENT_TYPES = frozenset(
    {"stock_cn", "etf_cn", "index_cn", "fund_cn", "unsupported_cn"}
)

# The ordinary application runtime owns a deliberately small identity catalog.
# These are the same explicit symbols as the focused real-data validation
# profile; keeping the tuple here makes cold startup independent from a large
# on-disk catalog and, critically, from ``ExchangeQMT.all_stocks()``.
DEFAULT_VALIDATION_SYMBOL_CODES = (
    "SZ.000932",
    "SZ.000923",
    "SH.600516",
    "SZ.001203",
    "SZ.000783",
    "SZ.000987",
    "SH.601377",
    "SH.601628",
    "SZ.002377",
    "SH.601808",
    "SZ.000698",
    "SH.600583",
)
BOUNDED_VALIDATION_CATALOG = "BOUNDED_VALIDATION_CATALOG"
FULL_IDENTITY_CATALOG = "FULL_IDENTITY_CATALOG"

# Secure process defaults.  ``create_app`` re-applies its effective config
# before any preload thread can start, so stale module/cache state from another
# app factory cannot silently widen the catalog.
_symbol_catalog_mode = BOUNDED_VALIDATION_CATALOG
_symbol_catalog_codes_by_exchange = {
    "a": DEFAULT_VALIDATION_SYMBOL_CODES,
}
_full_catalog_refresh_authorized = False

# 全部支持的市场（用于校验配置项）
_ALL_PRELOAD_EXCHANGES = [
    "a", "hk", "fx", "us", "futures", "ny_futures", "currency", "currency_spot",
]


def _resolve_preload_exchanges():
    """从 config 读取 PRELOAD_MARKETS（list[str]）。
    - 未配置时回退默认仅预加载 a/hk/us（避免 futures/currency 这类境外/超时频发的市场拖慢启动）。
    - 配置中包含未知市场名时仅记 warning，不中断。
    用户用不到的市场可在 config.py 中显式置空 PRELOAD_MARKETS = [] 关闭预加载。
    """
    raw = getattr(config, "PRELOAD_MARKETS", None)
    if raw is None:
        return ["a", "hk", "us"]
    if not isinstance(raw, (list, tuple)):
        LogUtil.warning(
            f"config.PRELOAD_MARKETS 类型应为 list, 当前为 {type(raw).__name__}, "
            f"已忽略并使用默认值"
        )
        return ["a", "hk", "us"]
    valid = []
    for ex in raw:
        if ex in _ALL_PRELOAD_EXCHANGES:
            valid.append(ex)
        else:
            LogUtil.warning(f"config.PRELOAD_MARKETS 包含未知市场 {ex}, 已跳过")
    return valid


PRELOAD_EXCHANGES = _resolve_preload_exchanges()
PRELOAD_INTERVAL_SECONDS = 3600
# 启动后延迟多少秒才开始第一轮预加载，让启动初期完全静默。
# 可通过 config.PRELOAD_STARTUP_DELAY_SECONDS 覆盖，默认 30s。
PRELOAD_STARTUP_DELAY_SECONDS = max(0, int(getattr(config, "PRELOAD_STARTUP_DELAY_SECONDS", 30)))
# 并发度自适应：不超过待加载市场数量，也不超过原上限 8。
PRELOAD_PARALLEL_WORKERS = min(8, max(1, len(PRELOAD_EXCHANGES))) if PRELOAD_EXCHANGES else 1

# 单市场单次刷新的最大耗时阈值，仅用于日志告警，不会强制 kill 任务。
_PRELOAD_SLOW_WARN_SECONDS = 10
PRELOAD_ATTEMPT_TIMEOUT_SECONDS = max(
    0.1, float(getattr(config, "PRELOAD_ATTEMPT_TIMEOUT_SECONDS", 15.0))
)

# 正在异步刷新的市场集合，防止同一市场被并发触发多次刷新而堆积慢请求。
_async_refresh_in_flight: set = set()
_async_refresh_lock = threading.Lock()
# 各市场独立保存刷新状态，并共用缓存锁，使缓存内容与就绪快照能够被原子观测。
_symbol_states = {}
# 磁盘缓存的来源与新鲜度只作为“首轮是否可跳过重复刷新”的证据，不参与
# 运行权限判定。旧缓存仍可作为 LKG 恢复，但缺少当前元数据时绝不会冒充
# 刚生成的全量目录。
_disk_cache_metadata = {}
_preload_attempts = {}
_preload_attempts_lock = threading.Lock()
_preload_handle_lock = threading.Lock()
_preload_handle = None
_symbol_runtime_closed = False


def _catalog_scope_snapshot(exchange: str) -> tuple[str, tuple[str, ...], bool]:
    """Return the immutable catalog scope without performing external I/O."""

    with _stock_cache_lock:
        return (
            _symbol_catalog_mode,
            tuple(_symbol_catalog_codes_by_exchange.get(exchange, ())),
            _full_catalog_refresh_authorized,
        )


def _project_rows_to_catalog_scope(exchange: str, rows):
    """Project restored/last-known rows onto the current bounded admission."""

    mode, admitted_codes, _authorized = _catalog_scope_snapshot(exchange)
    if mode == FULL_IDENTITY_CATALOG:
        return list(rows or ())
    admitted = set(admitted_codes)
    if not admitted:
        return []
    projected = []
    seen = set()
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        code = str(row.get("code") or "").strip()
        if code not in admitted or code in seen:
            continue
        projected.append(row)
        seen.add(code)
    return projected


def configure_symbol_catalog(
    *,
    validation_codes=None,
    full_catalog_authorized: bool = False,
) -> dict[str, object]:
    """Install the process catalog scope before any preload work starts.

    Ordinary mode always admits an explicit A-share cohort capped at twelve.
    Full identity enumeration is a separate boolean authorization; it is never
    inferred from screening/full-coverage settings.
    """

    global _symbol_catalog_mode, _symbol_catalog_codes_by_exchange
    global _full_catalog_refresh_authorized

    if type(full_catalog_authorized) is not bool:
        raise TypeError("full_catalog_authorized must be an exact bool")
    values = validation_codes
    if values is None or (isinstance(values, str) and not values.strip()):
        values = DEFAULT_VALIDATION_SYMBOL_CODES
    admitted_codes = admit_explicit_validation_codes(
        values,
        max_symbols=DEFAULT_VALIDATION_COHORT_SIZE,
    )
    invalid_codes = tuple(
        code for code in admitted_codes if _A_STOCK_CODE.fullmatch(code) is None
    )
    if invalid_codes:
        raise ValueError(
            "validation symbol catalog accepts only normalized A-share codes: "
            + ", ".join(invalid_codes)
        )

    mode = (
        FULL_IDENTITY_CATALOG
        if full_catalog_authorized
        else BOUNDED_VALIDATION_CATALOG
    )
    with _stock_cache_lock:
        _symbol_catalog_mode = mode
        _full_catalog_refresh_authorized = full_catalog_authorized
        _symbol_catalog_codes_by_exchange = {"a": tuple(admitted_codes)}
        if not full_catalog_authorized:
            for exchange in tuple(stock_cache):
                allowed = set(_symbol_catalog_codes_by_exchange.get(exchange, ()))
                projected = [
                    row
                    for row in (stock_cache.get(exchange) or ())
                    if isinstance(row, dict) and row.get("code") in allowed
                ]
                if projected:
                    stock_cache[exchange] = projected
                else:
                    stock_cache.pop(exchange, None)
                _symbol_states.pop(exchange, None)
    return {
        "catalog_mode": mode,
        "admitted_codes": tuple(admitted_codes),
        "admitted_count": len(admitted_codes),
        "full_catalog_authorized": full_catalog_authorized,
    }


def _mark_symbol_ready(exchange: str) -> None:
    with _stock_cache_lock:
        _symbol_states[exchange] = {"status": "ready", "last_error": None}


def _mark_symbol_degraded(exchange: str, error: str) -> None:
    with _stock_cache_lock:
        _symbol_states[exchange] = {"status": "degraded", "last_error": error}


def _cached_symbols_or_empty(exchange: str):
    with _stock_cache_lock:
        return stock_cache.get(exchange) or []


def get_cached_processed_stock(exchange: str, code: str):
    """无外部 I/O 地返回一个缓存标的。

    ``/tv/symbols`` 位于图表启动关键路径。QMT 全市场刷新会长时间持有原生锁，此时
    回退到 ``exchange.stock_info`` 可能超过数据源十五秒上限；磁盘恢复缓存已经包含
    立即解析图表所需的元数据。

    返回副本，避免调用方在规范化 ``precision`` 等可选字段时修改进程级最后有效值。
    """
    normalized_code = str(code or "").strip()
    if not normalized_code:
        return None
    with _stock_cache_lock:
        cached = stock_cache.get(exchange) or ()
        for stock in cached:
            if stock.get("code") == normalized_code:
                return stock.copy()
    return None


def get_cached_a_instrument_types(codes: tuple[str, ...]) -> dict[str, str]:
    """从已恢复的唯一 A 股证券目录读取精确类型，不触发 QMT 或磁盘 I/O。

    目录由显式准入标的的身份查询生成并原子持久化。缺失、冲突或
    非现行类型一律返回 ``unresolved_cn``，让选股范围按失败关闭处理。
    """

    if type(codes) is not tuple or any(
        type(code) is not str or _A_STOCK_CODE.fullmatch(code) is None
        for code in codes
    ):
        raise TypeError("codes must be an exact normalized A-share tuple")
    if len(codes) != len(set(codes)) or tuple(sorted(codes)) != codes:
        raise ValueError("codes must be unique and sorted")
    requested = set(codes)
    resolved: dict[str, str] = {}
    conflicts: set[str] = set()
    with _stock_cache_lock:
        cached = tuple(stock_cache.get("a") or ())
    for row in cached:
        if not isinstance(row, dict):
            continue
        code = row.get("code")
        kind = row.get("type")
        if code not in requested or kind not in _KNOWN_A_INSTRUMENT_TYPES:
            continue
        previous = resolved.get(code)
        if previous is not None and previous != kind:
            conflicts.add(code)
            continue
        resolved[code] = kind
    return {
        code: (
            "unresolved_cn"
            if code in conflicts or code not in resolved
            else resolved[code]
        )
        for code in codes
    }


def get_cached_a_symbol_names(codes: tuple[str, ...]) -> dict[str, str | None]:
    """从已恢复的 A 股证券目录读取名称，不触发 QMT 或磁盘 I/O。"""

    if type(codes) is not tuple or any(
        type(code) is not str or _A_STOCK_CODE.fullmatch(code) is None
        for code in codes
    ):
        raise TypeError("codes must be an exact normalized A-share tuple")
    if len(codes) != len(set(codes)) or tuple(sorted(codes)) != codes:
        raise ValueError("codes must be unique and sorted")
    requested = set(codes)
    resolved: dict[str, str] = {}
    conflicts: set[str] = set()
    with _stock_cache_lock:
        cached = tuple(stock_cache.get("a") or ())
    for row in cached:
        if not isinstance(row, dict):
            continue
        code = row.get("code")
        raw_name = row.get("name")
        if code not in requested or not isinstance(raw_name, str):
            continue
        name = raw_name.strip()
        if not name:
            continue
        previous = resolved.get(code)
        if previous is not None and previous != name:
            conflicts.add(code)
            continue
        resolved[code] = name
    return {
        code: None if code in conflicts else resolved.get(code)
        for code in codes
    }


def get_symbol_readiness(exchange: str):
    """Return an in-memory snapshot without triggering a refresh or external I/O."""
    with _stock_cache_lock:
        cached = stock_cache.get(exchange)
        state = _symbol_states.get(exchange)
        catalog_mode = _symbol_catalog_mode
        admitted_codes = tuple(_symbol_catalog_codes_by_exchange.get(exchange, ()))
        full_catalog_authorized = _full_catalog_refresh_authorized
        admitted_count = len(admitted_codes)
        if exchange not in PRELOAD_EXCHANGES and not cached:
            return {
                "market": exchange,
                "ready": True,
                "status": "disabled",
                "count": 0,
                "last_error": None,
                "catalog_mode": catalog_mode,
                "admitted_count": admitted_count,
                "full_catalog_authorized": full_catalog_authorized,
            }
        ready = bool(cached)
        if state is None:
            status = (
                "bounded_deferred"
                if catalog_mode == BOUNDED_VALIDATION_CATALOG
                and not admitted_codes
                else "ready" if ready else "not_ready"
            )
            last_error = None
        else:
            status = state["status"]
            last_error = state["last_error"]
            if status == "ready" and not ready:
                status = "not_ready"
        return {
            "market": exchange,
            "ready": ready,
            "status": status,
            "count": len(cached) if cached else 0,
            "last_error": last_error,
            "catalog_mode": catalog_mode,
            "admitted_count": admitted_count,
            "full_catalog_authorized": full_catalog_authorized,
        }


def _stocks_cache_dir() -> str:
    """返回 stocks 落盘缓存目录，缺失则创建。

    复用 ``config.get_data_path()`` 作为基础路径，与 chart 缓存等保持一致；
    创建失败仅记 warning 并返回原路径——上层 _save 会再 try/except 一次。
    """
    base = config.get_data_path()
    cache_dir = os.path.join(str(base), "cache", "symbols")
    try:
        os.makedirs(cache_dir, exist_ok=True)
    except OSError as e:
        LogUtil.warning(f"[stocks_cache] mkdir 失败 {cache_dir}: {e}")
    return cache_dir


def _stocks_cache_file(exchange: str) -> str:
    return os.path.join(_stocks_cache_dir(), f"{exchange}.json")


def _load_stocks_from_disk(exchange: str):
    """从落盘文件读取 raw stocks 列表；任何错误返回 None，调用方走原路径。"""
    path = _stocks_cache_file(exchange)
    with _stock_cache_lock:
        _disk_cache_metadata.pop(exchange, None)
    try:
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        if data.get("schema") != _STOCKS_CACHE_SCHEMA:
            LogUtil.info(
                f"[stocks_cache] {path} schema={data.get('schema')!r} "
                f"与当前契约不符，忽略"
            )
            return None
        if data.get("market") != exchange:
            LogUtil.warning(
                f"[stocks_cache] {path} market={data.get('market')!r} "
                f"与目标 {exchange!r} 不符，忽略"
            )
            return None
        stocks = data.get("stocks")
        if not isinstance(stocks, list) or not stocks:
            return None
        declared_count = data.get("count")
        updated_at = data.get("updated_at")
        catalog_mode = data.get("catalog_mode")
        scope_codes = data.get("scope_codes")
        metadata_verified = (
            type(declared_count) is int
            and declared_count == len(stocks)
            and type(updated_at) is int
            and updated_at > 0
            and catalog_mode in {
                BOUNDED_VALIDATION_CATALOG,
                FULL_IDENTITY_CATALOG,
            }
            and isinstance(scope_codes, list)
            and all(type(code) is str and code for code in scope_codes)
        )
        with _stock_cache_lock:
            _disk_cache_metadata[exchange] = {
                "verified": metadata_verified,
                "catalog_mode": catalog_mode,
                "updated_at": updated_at if type(updated_at) is int else 0,
                "count": declared_count if type(declared_count) is int else 0,
                "scope_codes": tuple(scope_codes) if isinstance(scope_codes, list) else (),
            }
        return stocks
    except Exception as e:
        LogUtil.warning(f"[stocks_cache] 读 {path} 失败: {e}")
        return None


def _save_stocks_to_disk(exchange: str, raw_stocks) -> None:
    """原子写 raw stocks 到落盘文件。空列表跳过（避免覆盖已有的好缓存）。

    只持久化 ``code`` / ``name`` / ``type`` 三个原始字段——A 股列表过滤依赖
    ``type``，而 ``code_lower`` / ``pinyin_initials`` 等 processed 字段会在恢复时由
    ``_process_stock_list`` 重新计算，避免文件膨胀和未来 processed schema 变更带来的兼容性坑。
    """
    if not raw_stocks:
        return
    path = _stocks_cache_file(exchange)
    catalog_mode, admitted_codes, _authorized = _catalog_scope_snapshot(exchange)
    updated_at = int(time.time())
    payload = {
        "schema": _STOCKS_CACHE_SCHEMA,
        "market": exchange,
        "updated_at": updated_at,
        "count": len(raw_stocks),
        "catalog_mode": catalog_mode,
        "scope_codes": (
            list(admitted_codes)
            if catalog_mode == BOUNDED_VALIDATION_CATALOG
            else []
        ),
        "stocks": [
            {
                "code": s.get("code", ""),
                "name": s.get("name", ""),
                "type": s.get("type", "unknown"),
            }
            for s in raw_stocks
        ],
    }
    tmp_path = None
    try:
        dir_ = os.path.dirname(path)
        # delete=False：上下文退出时不删除，由我们自己 os.replace；异常分支才删。
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".tmp",
            dir=dir_,
            delete=False,
        ) as tf:
            json.dump(payload, tf, ensure_ascii=False)
            tmp_path = tf.name
        os.replace(tmp_path, path)
        tmp_path = None  # 已替换成功，无需清理
        with _stock_cache_lock:
            _disk_cache_metadata[exchange] = {
                "verified": True,
                "catalog_mode": catalog_mode,
                "updated_at": updated_at,
                "count": len(raw_stocks),
                "scope_codes": (
                    tuple(admitted_codes)
                    if catalog_mode == BOUNDED_VALIDATION_CATALOG
                    else ()
                ),
            }
    except Exception as e:
        LogUtil.warning(f"[stocks_cache] 写 {path} 失败: {e}")
        if tmp_path and os.path.isfile(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _warm_cache_from_disk() -> None:
    """启动期同步从落盘文件恢复 stocks 缓存。

    设计要点：
    - 文件命中：当场把 processed 后的列表写入 ``stock_cache``，首次 ``/symbols/list``
      或 ``/tv/search`` 直接命中，不再触发 28s 同步兜底；之后预加载线程仍会异步
      跑一遍把数据刷新成最新。
    - 文件缺失/损坏：静默跳过，回退到原有 preload + sync fallback 路径。
    - 异常被吞掉：启动路径绝不能因为缓存损坏而 raise，宁可慢也不能挂。
    """
    if not PRELOAD_EXCHANGES:
        return
    for exchange in PRELOAD_EXCHANGES:
        catalog_mode, admitted_codes, _authorized = _catalog_scope_snapshot(exchange)
        if catalog_mode == BOUNDED_VALIDATION_CATALOG and not admitted_codes:
            with _stock_cache_lock:
                stock_cache.pop(exchange, None)
                _symbol_states[exchange] = {
                    "status": "bounded_deferred",
                    "last_error": None,
                }
            continue
        raw_stocks = _load_stocks_from_disk(exchange)
        if not raw_stocks:
            continue
        try:
            scoped_stocks = _project_rows_to_catalog_scope(exchange, raw_stocks)
            if not scoped_stocks:
                continue
            processed = _process_stock_list(scoped_stocks)
            if not processed:
                _mark_symbol_degraded(exchange, "empty symbol list")
                continue
            with _stock_cache_lock:
                # 防御：极端情况下 preload 已经先把缓存填了，则不覆盖；磁盘数据可能更陈旧。
                if not stock_cache.get(exchange):
                    stock_cache[exchange] = processed
                _symbol_states[exchange] = {"status": "ready", "last_error": None}
            LogUtil.info(
                f"[stocks_cache] 从磁盘恢复 {exchange} stocks，共 {len(processed)} 条"
            )
            if len(scoped_stocks) != len(raw_stocks):
                # Atomically replace a legacy broad catalog with its admitted
                # projection so a later restart cannot resurrect out-of-scope
                # identities even if its in-memory cache is empty.
                _save_stocks_to_disk(exchange, scoped_stocks)
        except Exception as e:
            _mark_symbol_degraded(exchange, str(e) or type(e).__name__)
            LogUtil.warning(f"[stocks_cache] 恢复 {exchange} 失败: {e}")


def _authorized_full_catalog_rows(ex, exchange: str):
    """Enumerate a market only while the independent authorization is live."""

    mode, _admitted_codes, authorized = _catalog_scope_snapshot(exchange)
    if mode != FULL_IDENTITY_CATALOG or not authorized:
        raise PermissionError(
            "full identity catalog enumeration is not independently authorized"
        )
    if getattr(ex, "all_stocks_requires_explicit_authorization", False) is True:
        return ex.all_stocks(full_market_authorized=True)
    return ex.all_stocks()


def _process_stock_list(all_stocks):
    processed_list = []
    for stock in all_stocks:
        stock_copy = stock.copy()
        stock_copy['code_lower'] = stock['code'].lower()
        stock_copy['name_lower'] = stock['name'].lower()
        try:
            stock_copy['pinyin_initials'] = "".join([
                pinyin.get_initial(_p)[0] for _p in stock["name"]
            ]).lower()
        except Exception:
            stock_copy['pinyin_initials'] = ""
        processed_list.append(stock_copy)
    return processed_list


def _recent_full_disk_cache_covers_scope(exchange: str, cached) -> bool:
    """Return whether a disk-restored full catalog can skip one startup refresh.

    Runtime full-catalog authorization remains mandatory and is never restored
    from disk.  The cache must additionally carry current provenance, an exact
    row count, and a timestamp no older than one refresh interval.  Periodic
    rounds still refresh normally because this gate is used only when
    ``skip_if_disk_warm`` is true.
    """

    if not cached:
        return False
    with _stock_cache_lock:
        metadata = dict(_disk_cache_metadata.get(exchange) or {})
    if (
        metadata.get("verified") is not True
        or metadata.get("catalog_mode") != FULL_IDENTITY_CATALOG
        or metadata.get("count") != len(cached)
    ):
        return False
    updated_at = metadata.get("updated_at")
    if type(updated_at) is not int or updated_at <= 0:
        return False
    age_seconds = time.time() - updated_at
    freshness_seconds = max(60.0, float(PRELOAD_INTERVAL_SECONDS))
    return -60.0 <= age_seconds <= freshness_seconds


def _bounded_stock_info_rows(ex, exchange: str, admitted_codes: tuple[str, ...]):
    """Resolve only explicitly admitted identities, never a market catalog."""

    with _stock_cache_lock:
        existing = {
            row.get("code"): row.copy()
            for row in (stock_cache.get(exchange) or ())
            if isinstance(row, dict) and isinstance(row.get("code"), str)
        }
    rows = []
    unresolved = []
    fresh_count = 0
    for code in admitted_codes:
        try:
            info = resolve_bounded_stock_info(
                ex,
                code,
                fallback_name=existing.get(code, {}).get("name"),
                allow_code_fallback=True,
            )
        except Exception as exc:
            unresolved.append(f"{code}: {type(exc).__name__}: {str(exc)[:120]}")
            if code in existing:
                rows.append(existing[code])
            continue
        if not isinstance(info, Mapping):
            unresolved.append(f"{code}: identity unavailable")
            if code in existing:
                rows.append(existing[code])
            continue
        name = str(info.get("name") or "").strip()
        if not name:
            unresolved.append(f"{code}: identity name unavailable")
            if code in existing:
                rows.append(existing[code])
            continue
        row = dict(info)
        row["code"] = code
        row["name"] = name
        instrument_type = row.get("type")
        if exchange == "a" and instrument_type not in _KNOWN_A_INSTRUMENT_TYPES:
            previous_type = existing.get(code, {}).get("type")
            row["type"] = (
                previous_type
                if previous_type in _KNOWN_A_INSTRUMENT_TYPES
                else "stock_cn"
            )
        elif not isinstance(instrument_type, str) or not instrument_type:
            row["type"] = str(existing.get(code, {}).get("type") or "unknown")
        rows.append(row)
        fresh_count += 1
    return rows, tuple(unresolved), fresh_count


def _preload_single_exchange(exchange: str, skip_if_disk_warm: bool = False) -> None:
    """加载单个市场的 symbol 列表并写入缓存。任何异常都被吞掉，仅记日志，避免影响其他市场。

    ``skip_if_disk_warm=True`` 时，若 ``stock_cache[exchange]`` 已经被
    ``_warm_cache_from_disk`` 填充，则跳过本次抓取（首轮启动避免与磁盘恢复
    重复劳动）。后续轮次仍按 ``PRELOAD_INTERVAL_SECONDS`` 节奏刷新证券身份目录，
    保证缓存最终一致；这里只读取代码/名称元数据，不运行逐股策略。
    ``_trigger_async_refresh`` 走默认 False，行为不变。
    """
    catalog_mode, admitted_codes, full_catalog_authorized = (
        _catalog_scope_snapshot(exchange)
    )
    if catalog_mode == BOUNDED_VALIDATION_CATALOG and not admitted_codes:
        with _stock_cache_lock:
            stock_cache.pop(exchange, None)
            _symbol_states[exchange] = {
                "status": "bounded_deferred",
                "last_error": None,
            }
        return
    if skip_if_disk_warm:
        with _stock_cache_lock:
            cached = stock_cache.get(exchange)
        cached_codes = {
            row.get("code")
            for row in (cached or ())
            if isinstance(row, dict)
        }
        bounded_cache_covers_scope = bool(cached) and (
            catalog_mode == BOUNDED_VALIDATION_CATALOG
            and set(admitted_codes).issubset(cached_codes)
        )
        full_cache_covers_scope = (
            catalog_mode == FULL_IDENTITY_CATALOG
            and full_catalog_authorized
            and _recent_full_disk_cache_covers_scope(exchange, cached)
        )
        cache_covers_scope = bounded_cache_covers_scope or full_cache_covers_scope
        if cache_covers_scope:
            _mark_symbol_ready(exchange)
            cache_kind = "近期全量目录" if full_cache_covers_scope else "准入目录"
            LogUtil.info(
                f"市场 {exchange} 已由磁盘恢复{cache_kind} {len(cached)} 条，"
                f"跳过本轮预加载，"
                f"下一轮按 PRELOAD_INTERVAL_SECONDS 定时刷新"
            )
            return
    try:
        start_ts = time.time()
        ex = get_exchange(Market(exchange))
        # 短路：如果交易所实例标记了 init_failed（例如通达信连接超时），
        # 直接跳过身份查询，避免再次阻塞。
        if getattr(ex, "init_failed", False):
            _mark_symbol_degraded(exchange, "exchange init failed")
            LogUtil.warning(f"市场 {exchange} 交易所初始化失败，跳过本次预加载")
            return
        unresolved = ()
        if catalog_mode == FULL_IDENTITY_CATALOG:
            if not full_catalog_authorized:
                raise RuntimeError(
                    "full identity catalog refresh requires independent authorization"
                )
            raw_stocks = _authorized_full_catalog_rows(ex, exchange)
            fresh_count = len(raw_stocks or ())
        else:
            raw_stocks, unresolved, fresh_count = _bounded_stock_info_rows(
                ex,
                exchange,
                admitted_codes,
            )
        if fresh_count <= 0:
            _mark_symbol_degraded(exchange, "empty symbol list")
            LogUtil.warning(
                f"市场 {exchange} 证券身份目录返回空列表，保留现有缓存"
            )
            return
        # Re-project immediately before publication.  If another app factory
        # narrows the process scope while a refresh is in flight, its old
        # result cannot repopulate out-of-scope identities.
        raw_stocks = _project_rows_to_catalog_scope(exchange, raw_stocks)
        processed_stocks = _process_stock_list(raw_stocks)
        if not processed_stocks:
            _mark_symbol_degraded(exchange, "empty symbol list")
            return
        with _stock_cache_lock:
            stock_cache[exchange] = processed_stocks
            _symbol_states[exchange] = {
                "status": "degraded" if unresolved else "ready",
                "last_error": (
                    f"{len(unresolved)} admitted identities unresolved"
                    if unresolved
                    else None
                ),
            }
        # 写盘：让下次冷启动直接秒读文件，不再等 28s 通达信。
        # 失败仅 warn 不影响主流程；空身份结果会被落盘函数内部短路。
        _save_stocks_to_disk(exchange, raw_stocks)
        elapsed = time.time() - start_ts
        log_fn = LogUtil.warning if elapsed > _PRELOAD_SLOW_WARN_SECONDS else LogUtil.info
        log_fn(
            f"市场 {exchange} 证券身份目录缓存完成，共 {len(processed_stocks)} 条，"
            f"catalog_mode={catalog_mode} admitted={len(admitted_codes)}，"
            f"未运行逐股策略，耗时 {elapsed:.2f}s"
        )
    except Exception as e:
        _mark_symbol_degraded(exchange, str(e) or type(e).__name__)
        LogUtil.error(f"加载市场 {exchange} 证券身份目录失败: {e}")


class SymbolPreloadHandle:
    """Stoppable singleton handle for the periodic symbol preload loop."""

    def __init__(self, stop_event, thread):
        self._stop_event = stop_event
        self._thread = thread

    def stop(self):
        self._stop_event.set()

    def join(self, timeout=None):
        self._thread.join(timeout)

    def is_alive(self):
        return self._thread.is_alive()

    @property
    def name(self):
        return self._thread.name


def _start_preload_attempt(exchange: str, *, skip_if_disk_warm: bool = False):
    """Start at most one daemon refresh attempt for a market."""
    with _preload_attempts_lock:
        existing = _preload_attempts.get(exchange)
        if existing is not None:
            return existing
        if _symbol_runtime_closed:
            return None

        done = threading.Event()
        attempt = {
            "done": done,
            "thread": None,
            "started_at": time.monotonic(),
            "timed_out": False,
        }

        def _worker():
            try:
                _preload_single_exchange(
                    exchange,
                    skip_if_disk_warm=skip_if_disk_warm,
                )
            finally:
                done.set()
                with _preload_attempts_lock:
                    if _preload_attempts.get(exchange) is attempt:
                        _preload_attempts.pop(exchange, None)
                with _async_refresh_lock:
                    _async_refresh_in_flight.discard(exchange)

        thread = threading.Thread(
            target=_worker,
            daemon=True,
            name=f"SymbolRefresh-{exchange}",
        )
        attempt["thread"] = thread
        _preload_attempts[exchange] = attempt
        with _async_refresh_lock:
            _async_refresh_in_flight.add(exchange)
        thread.start()
        return attempt


def _mark_preload_timeout(exchange: str, attempt) -> None:
    with _preload_attempts_lock:
        if attempt.get("timed_out"):
            return
        attempt["timed_out"] = True
    seconds = max(0.1, float(PRELOAD_ATTEMPT_TIMEOUT_SECONDS))
    _mark_symbol_degraded(exchange, f"refresh timeout after {seconds:g}s")
    LogUtil.warning(f"市场 {exchange} symbols 刷新超过 {seconds:g}s，保留现有缓存")


def _run_preload_round(stop_event=None, *, skip_if_disk_warm: bool = False) -> bool:
    attempts = []
    for exchange in PRELOAD_EXCHANGES:
        attempt = _start_preload_attempt(
            exchange,
            skip_if_disk_warm=skip_if_disk_warm,
        )
        if attempt is not None:
            attempts.append((exchange, attempt))

    timeout = max(0.1, float(PRELOAD_ATTEMPT_TIMEOUT_SECONDS))
    for exchange, attempt in attempts:
        deadline = attempt["started_at"] + timeout
        while not attempt["done"].is_set():
            if stop_event is not None and stop_event.is_set():
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _mark_preload_timeout(exchange, attempt)
                break
            attempt["done"].wait(min(0.05, remaining))
    return stop_event is None or not stop_event.is_set()


def preload_symbols(stop_event=None):
    """Periodically refresh configured markets without waiting forever on one market."""
    if not PRELOAD_EXCHANGES:
        LogUtil.info("config.PRELOAD_MARKETS 为空，跳过 symbols 预加载线程")
        return

    if PRELOAD_STARTUP_DELAY_SECONDS > 0:
        if stop_event is None:
            time.sleep(PRELOAD_STARTUP_DELAY_SECONDS)
        elif stop_event.wait(PRELOAD_STARTUP_DELAY_SECONDS):
            return

    first_round = True
    while stop_event is None or not stop_event.is_set():
        LogUtil.info("开始预加载并更新所有市场的 symbols（有界并行）...")
        round_start = time.time()
        _run_preload_round(
            stop_event,
            skip_if_disk_warm=first_round,
        )
        first_round = False
        LogUtil.info(
            f"本轮 symbols 预加载调度完成，总耗时 {time.time() - round_start:.2f}s"
        )
        if stop_event is None:
            time.sleep(PRELOAD_INTERVAL_SECONDS)
        elif stop_event.wait(PRELOAD_INTERVAL_SECONDS):
            return


def start_symbol_preload_thread():
    """Start the periodic preload once and return a stoppable shared handle."""
    global _preload_handle, _symbol_runtime_closed
    with _preload_handle_lock:
        if _preload_handle is not None and _preload_handle.is_alive():
            return _preload_handle
        _symbol_runtime_closed = False
        _warm_cache_from_disk()
        stop_event = threading.Event()
        thread = threading.Thread(
            target=preload_symbols,
            args=(stop_event,),
            daemon=True,
            name="SymbolPreloadThread",
        )
        handle = SymbolPreloadHandle(stop_event, thread)
        _preload_handle = handle
        thread.start()
        return handle


def shutdown_symbol_preload(timeout=1.0):
    """Stop periodic scheduling; hung exchange calls remain bounded daemon attempts."""
    global _preload_handle, _symbol_runtime_closed
    with _preload_handle_lock:
        _symbol_runtime_closed = True
        handle = _preload_handle
    if handle is None:
        return True
    handle.stop()
    handle.join(max(0.0, float(timeout)))
    stopped = not handle.is_alive()
    if stopped:
        with _preload_handle_lock:
            if _preload_handle is handle:
                _preload_handle = None
    return stopped

def _trigger_async_refresh(exchange: str) -> None:
    """Start a single bounded, observable refresh attempt for one market."""
    attempt = _start_preload_attempt(exchange)
    if attempt is None:
        return
    with _preload_attempts_lock:
        if attempt.get("watchdog_started"):
            return
        attempt["watchdog_started"] = True

    def _watchdog():
        timeout = max(0.1, float(PRELOAD_ATTEMPT_TIMEOUT_SECONDS))
        if not attempt["done"].wait(timeout):
            _mark_preload_timeout(exchange, attempt)

    threading.Thread(
        target=_watchdog,
        daemon=True,
        name=f"SymbolRefreshWatchdog-{exchange}",
    ).start()

def get_cached_processed_stocks(exchange, allow_sync_fallback: bool = False):
    """获取指定市场已缓存的 symbols 列表。

    设计要点（启动慢优化）:
    - 缓存命中: 直接返回。
    - 缓存 miss + ``allow_sync_fallback=False``: 触发后台异步刷新并 raise，适合那些"宁可
      报错也不能阻塞"的入口（如图表页面初始化时的 symbol_info 探测）。
    - 缓存 miss + ``allow_sync_fallback=True``: 同步等待一次显式准入身份查询并写入
      缓存；普通模式仍不会调用 ``all_stocks()`` 或展开市场目录。
    - 同步路径里任何异常（包括交易所连接超时）都吞掉并返回 ``[]``，搜索框最差是"无结果"。
    """
    with _stock_cache_lock:
        cached = stock_cache.get(exchange)
    if cached:
        return cached

    if not allow_sync_fallback:
        _trigger_async_refresh(exchange)
        raise RuntimeError(
            f"市场 {exchange} symbols 尚未就绪（后台正在加载，请稍后重试）"
        )

    # 同步兜底也只等待一个明确期限；底层挂起 attempt 保持单例并在 daemon 中完成。
    attempt = _start_preload_attempt(exchange)
    if attempt is None:
        return _cached_symbols_or_empty(exchange)
    timeout = max(0.1, float(PRELOAD_ATTEMPT_TIMEOUT_SECONDS))
    if not attempt["done"].wait(timeout):
        _mark_preload_timeout(exchange, attempt)
        return _cached_symbols_or_empty(exchange)
    return _cached_symbols_or_empty(exchange)
