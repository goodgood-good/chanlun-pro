"""标的列表服务（service）。

设计要点：
- 启动后延迟 ``PRELOAD_STARTUP_DELAY_SECONDS`` 才开始第一轮，让首次访问页面
  不被预加载抢占资源；之后每 ``PRELOAD_INTERVAL_SECONDS`` 周期性刷新。
- 单市场预加载失败/超时不会影响其他市场（_preload_single_exchange 自吞异常）。
- 缓存 miss 时按调用方意愿走两条路径：
  - allow_sync_fallback=False: 触发异步刷新 + raise（适合不能阻塞的初始化路径）
  - allow_sync_fallback=True: 同步调一次（适合用户搜索，宁可慢也不能 500）

API：
- ``stock_cache``: TTLCache(100, 7200)，市场 → 处理后的 symbols 列表
- ``preload_symbols`` / ``start_symbol_preload_thread``: 后台预加载入口
- ``get_cached_processed_stocks(exchange, allow_sync_fallback=False)``: 读取入口
- ``_trigger_async_refresh(exchange)``: 单例化的异步刷新触发器
"""
import json
import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from cachetools import TTLCache
import pinyin

from chanlun import config
from chanlun.base import Market
from chanlun.exchange import get_exchange
from chanlun.tools.log_util import LogUtil

# 基础数据缓存（市场 → processed symbols list）
stock_cache: TTLCache = TTLCache(maxsize=100, ttl=7200)
# stock_cache 专用锁（M3）。曾与 chart_cache.cache_lock 共用一把锁，导致一次
# chart 缓存的磁盘读会阻塞 symbol 列表读写；此处独立成锁，两个无关缓存互不干扰。
_stock_cache_lock = threading.Lock()

# 落盘缓存格式版本：未来若 schema 变更（如新增字段、改 raw 为 processed），递增此值，
# 旧文件会被忽略并被新版本覆盖，避免反序列化时 KeyError。
_STOCKS_CACHE_VERSION = 1

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

# 正在异步刷新的市场集合，防止同一市场被并发触发多次刷新而堆积慢请求。
_async_refresh_in_flight: set = set()
_async_refresh_lock = threading.Lock()


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
    try:
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        if data.get("version") != _STOCKS_CACHE_VERSION:
            LogUtil.info(
                f"[stocks_cache] {path} version={data.get('version')!r} "
                f"与当前 {_STOCKS_CACHE_VERSION} 不符，忽略"
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
        return stocks
    except Exception as e:
        LogUtil.warning(f"[stocks_cache] 读 {path} 失败: {e}")
        return None


def _save_stocks_to_disk(exchange: str, raw_stocks) -> None:
    """原子写 raw stocks 到落盘文件。空列表跳过（避免覆盖已有的好缓存）。

    只持久化 ``code`` / ``name`` 两个原始字段——``code_lower`` / ``pinyin_initials``
    等 processed 字段会在恢复时由 ``_process_stock_list`` 重新计算，避免文件膨胀
    和未来 processed schema 变更带来的兼容性坑。
    """
    if not raw_stocks:
        return
    path = _stocks_cache_file(exchange)
    payload = {
        "version": _STOCKS_CACHE_VERSION,
        "market": exchange,
        "updated_at": int(time.time()),
        "count": len(raw_stocks),
        "stocks": [
            {"code": s.get("code", ""), "name": s.get("name", "")}
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
        raw_stocks = _load_stocks_from_disk(exchange)
        if not raw_stocks:
            continue
        try:
            processed = _process_stock_list(raw_stocks)
            with _stock_cache_lock:
                # 防御：极端情况下 preload 已经先把缓存填了，则不覆盖；磁盘数据可能更陈旧。
                if exchange not in stock_cache:
                    stock_cache[exchange] = processed
            LogUtil.info(
                f"[stocks_cache] 从磁盘恢复 {exchange} stocks，共 {len(processed)} 条"
            )
        except Exception as e:
            LogUtil.warning(f"[stocks_cache] 恢复 {exchange} 失败: {e}")


def _safe_all_stocks(ex, exchange: str):
    """统一的 all_stocks 调用入口。

    历史 bug：``ExchangeChangQiao`` 是 ``@fun.singleton``，A/HK/US 三个市场共享同一实例。
    如果只靠实例字段 ``default_market`` 区分市场，后注册的市场会覆盖前面的字段，结果
    所有市场拿到的都是最后一个市场的数据（实测复现）。所以这里强制把 ``exchange`` 作为
    参数传下去——cq 实现的 ``all_stocks`` 接受可选 ``market`` 参数；其它 exchange 的
    签名是无参的，用 try/except 优雅降级即可。
    """
    try:
        return ex.all_stocks(exchange)
    except TypeError:
        # 大多数非 cq 的 exchange 是无参签名，退回原行为。
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


def _preload_single_exchange(exchange: str, skip_if_disk_warm: bool = False) -> None:
    """加载单个市场的 symbol 列表并写入缓存。任何异常都被吞掉，仅记日志，避免影响其他市场。

    ``skip_if_disk_warm=True`` 时，若 ``stock_cache[exchange]`` 已经被
    ``_warm_cache_from_disk`` 填充，则跳过本次抓取（首轮启动避免与磁盘恢复
    重复劳动）。后续轮次仍按 ``PRELOAD_INTERVAL_SECONDS`` 节奏正常全量刷新，
    保证缓存最终一致。``_trigger_async_refresh`` 走默认 False，行为不变。
    """
    if skip_if_disk_warm:
        with _stock_cache_lock:
            cached = stock_cache.get(exchange)
        if cached:
            LogUtil.info(
                f"市场 {exchange} 已由磁盘恢复 {len(cached)} 条，跳过本轮预加载，"
                f"下一轮按 PRELOAD_INTERVAL_SECONDS 定时刷新"
            )
            return
    try:
        start_ts = time.time()
        ex = get_exchange(Market(exchange))
        # 短路：如果交易所实例标记了 init_failed（例如通达信连接超时），
        # 直接跳过 all_stocks 调用，避免再次 30s 阻塞。
        if getattr(ex, "init_failed", False):
            LogUtil.warning(f"市场 {exchange} 交易所初始化失败，跳过本次预加载")
            return
        all_stocks = _safe_all_stocks(ex, exchange)
        processed_stocks = _process_stock_list(all_stocks)
        with _stock_cache_lock:
            stock_cache[exchange] = processed_stocks
        # 写盘：让下次冷启动直接秒读文件，不再等 28s 通达信。
        # 失败仅 warn 不影响主流程；空 all_stocks 会被 _save_stocks_to_disk 内部短路。
        _save_stocks_to_disk(exchange, all_stocks)
        elapsed = time.time() - start_ts
        log_fn = LogUtil.warning if elapsed > _PRELOAD_SLOW_WARN_SECONDS else LogUtil.info
        log_fn(
            f"市场 {exchange} symbols 预加载完成，共 {len(processed_stocks)} 条，耗时 {elapsed:.2f}s"
        )
    except Exception as e:
        LogUtil.error(f"预加载市场 {exchange} symbols 失败: {e}")


def preload_symbols():
    """常驻线程入口：并行预加载配置中的市场 symbols，并周期性刷新。

    设计要点：
    1. 启动后先静默 PRELOAD_STARTUP_DELAY_SECONDS 秒再开始第一轮，让启动初期日志干净、
       不和用户首次访问页面争抢资源。
    2. 每轮使用 ThreadPoolExecutor 并行触发，单轮总耗时由最慢的市场决定。
    3. 单个市场失败不会影响其他市场。
    4. 该函数本身运行在 daemon 线程中，不阻塞主进程。
    5. 如果 PRELOAD_EXCHANGES 为空，直接退出线程，不再周期性刷新。
    """
    if not PRELOAD_EXCHANGES:
        LogUtil.info("config.PRELOAD_MARKETS 为空，跳过 symbols 预加载线程")
        return

    # 启动延迟：让 web 服务先静默就绪
    if PRELOAD_STARTUP_DELAY_SECONDS > 0:
        time.sleep(PRELOAD_STARTUP_DELAY_SECONDS)

    # 首轮特殊处理：磁盘恢复命中的市场跳过本次抓取，等下一轮 PRELOAD_INTERVAL_SECONDS
    # 到点再正常全量刷新。避免启动后立刻和 _warm_cache_from_disk 重复工作。
    first_round = True
    while True:
        LogUtil.info("开始预加载并更新所有市场的 symbols（并行）...")
        round_start = time.time()
        try:
            with ThreadPoolExecutor(
                max_workers=PRELOAD_PARALLEL_WORKERS,
                thread_name_prefix="SymbolPreloadWorker",
            ) as pool:
                # 提交后等待所有 future 结束（_preload_single_exchange 已自行吞异常）
                if first_round:
                    list(pool.map(
                        lambda ex: _preload_single_exchange(ex, skip_if_disk_warm=True),
                        PRELOAD_EXCHANGES,
                    ))
                else:
                    list(pool.map(_preload_single_exchange, PRELOAD_EXCHANGES))
        except Exception as e:
            LogUtil.error(f"symbols 预加载轮次异常: {e}")
        LogUtil.info(f"本轮 symbols 预加载完成，总耗时 {time.time() - round_start:.2f}s")

        first_round = False
        time.sleep(PRELOAD_INTERVAL_SECONDS)


def start_symbol_preload_thread():
    """启动后台 symbol 预加载线程。线程为 daemon，不会阻塞进程退出，也不会阻塞调用方。

    在起线程前先 **同步** 从落盘文件恢复一次 stocks 缓存——文件命中时首请求秒返回，
    后台线程仍然会跑一遍把缓存刷成最新。文件不存在时按原路径走，等预加载完成。
    """
    _warm_cache_from_disk()
    t = threading.Thread(target=preload_symbols, daemon=True, name="SymbolPreloadThread")
    t.start()
    return t


def _trigger_async_refresh(exchange: str) -> None:
    """触发后台线程异步刷新该市场的 symbols，不阻塞调用方。
    使用 _async_refresh_in_flight 集合做单例化，避免同一市场并发刷新堆积。
    """
    with _async_refresh_lock:
        if exchange in _async_refresh_in_flight:
            return
        _async_refresh_in_flight.add(exchange)

    def _worker():
        try:
            _preload_single_exchange(exchange)
        finally:
            with _async_refresh_lock:
                _async_refresh_in_flight.discard(exchange)

    t = threading.Thread(target=_worker, daemon=True, name=f"SymbolAsyncRefresh-{exchange}")
    t.start()


def get_cached_processed_stocks(exchange, allow_sync_fallback: bool = False):
    """获取指定市场已缓存的 symbols 列表。

    设计要点（启动慢优化）:
    - 缓存命中: 直接返回。
    - 缓存 miss + ``allow_sync_fallback=False``: 触发后台异步刷新并 raise，适合那些"宁可
      报错也不能阻塞"的入口（如图表页面初始化时的 symbol_info 探测）。
    - 缓存 miss + ``allow_sync_fallback=True``: 同步调用一次 ``ex.all_stocks()`` 直接构建
      并写入缓存，适合"用户主动点搜索"这种愿意等几秒的场景。否则启动后 60s 预加载空窗期内
      搜索接口会全 500，用户体感是"搜索框完全坏了"。
    - 同步路径里任何异常（包括交易所连接超时）都吞掉并返回 ``[]``，搜索框最差是"无结果"。
    """
    with _stock_cache_lock:
        cached = stock_cache.get(exchange)
    if cached is not None:
        return cached

    if not allow_sync_fallback:
        _trigger_async_refresh(exchange)
        raise RuntimeError(
            f"市场 {exchange} symbols 尚未就绪（后台正在加载，请稍后重试）"
        )

    # 同步兜底路径：直接构建一次。即使较慢（a/hk/us 通常 1~5s），也比让用户看到 500 好。
    try:
        ex = get_exchange(Market(exchange))
        if getattr(ex, "init_failed", False):
            LogUtil.warning(
                f"[get_cached_processed_stocks] {exchange} 交易所初始化失败，同步兜底返回空列表"
            )
            return []
        all_stocks = _safe_all_stocks(ex, exchange) or []
        processed = _process_stock_list(all_stocks)
        with _stock_cache_lock:
            stock_cache[exchange] = processed
        # 同步兜底成功后也写盘：下次冷启动可直接秒读，跟 preload 路径保持一致。
        _save_stocks_to_disk(exchange, all_stocks)
        LogUtil.info(
            f"[get_cached_processed_stocks] {exchange} 同步兜底加载完成，共 {len(processed)} 条"
        )
        return processed
    except Exception as e:
        LogUtil.error(
            f"[get_cached_processed_stocks] {exchange} 同步兜底加载失败: {e}"
        )
        return []
