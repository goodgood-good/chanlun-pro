import os
import time
import atexit
import weakref
import threading
import functools
from datetime import timedelta, datetime, time as datetime_time
from typing import Dict, List, Union
import pandas as pd
import pytz
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError
from tenacity import Retrying, stop_after_attempt, wait_exponential, retry_if_exception

from longbridge.openapi import Config, QuoteContext, TradeContext, Market, Period, \
    AdjustType, OrderSide, OrderType, TimeInForceType, SecurityListCategory, TradeSessions, OpenApiException

# 缠论软件开发工具包导入。
from chanlun import fun
from chanlun import config
from chanlun.exchange import Exchange
from chanlun.exchange.exchange import Tick
from chanlun.fun import str_to_datetime
from chanlun.tools.log_util import LogUtil
from chanlun.exchange.lb_quota_tracker import LbQuotaTracker
from chanlun.exchange.lb_priority import lb_low_priority, _lb_call_priority  # noqa: F401  (lb_low_priority 供 web 层 prewarm 标记)
from chanlun.exchange.kline_precision import (
    normalize_kline_precision,
    resolve_structure_price_quantum,
)
from chanlun.exchange.price_basis import (
    attach_price_basis_metadata,
    build_provider_price_basis_metadata,
)

# 统一时区设置
__tz = pytz.timezone("Asia/Shanghai")


def _get_env(key: str):
    # 生产配置只接受当前 LONGBRIDGE_* 命名空间。
    return os.getenv(key)


def _get_env_bool(key: str, default: bool = False) -> bool:
    # SDK 的布尔配置通常通过环境变量传入，这里统一做一次字符串到 bool 的转换。
    value = _get_env(key)
    if value is None:
        return default
    return value.lower() == "true"


def _env_int(key: str, default: int) -> int:
    """读取整型环境变量；非法或 <1 时回退默认值。用于限流 QPS 等可调参数。"""
    try:
        v = int(os.getenv(key, str(default)))
        return v if v >= 1 else default
    except (TypeError, ValueError):
        return default


def _build_longbridge_config() -> Config:
    """按当前长桥 SDK 与 LONGBRIDGE_* 环境变量构建配置。"""
    app_key = _get_env("LONGBRIDGE_APP_KEY")
    app_secret = _get_env("LONGBRIDGE_APP_SECRET")
    access_token = _get_env("LONGBRIDGE_ACCESS_TOKEN")

    http_url = _get_env("LONGBRIDGE_HTTP_URL")
    quote_ws_url = _get_env("LONGBRIDGE_QUOTE_WS_URL")
    trade_ws_url = _get_env("LONGBRIDGE_TRADE_WS_URL")
    enable_overnight = _get_env_bool("LONGBRIDGE_ENABLE_OVERNIGHT", False)
    enable_print_quote_packages = _get_env_bool(
        "LONGBRIDGE_PRINT_QUOTE_PACKAGES", True
    )
    log_path = _get_env("LONGBRIDGE_LOG_PATH")

    # 显式凭证允许同时传入接入点与运行开关。
    credentials = (app_key, app_secret, access_token)
    if all(credentials):
        return Config.from_apikey(
            app_key,
            app_secret,
            access_token,
            http_url=http_url,
            quote_ws_url=quote_ws_url,
            trade_ws_url=trade_ws_url,
            enable_overnight=enable_overnight,
            enable_print_quote_packages=enable_print_quote_packages,
            log_path=log_path,
        )

    if any(credentials):
        raise RuntimeError(
            "Longbridge credentials must be configured as a complete set"
        )
    return Config.from_apikey_env()


class RateLimiter:
    """
    简单令牌桶限流器 (Simple Token Bucket)
    """
    def __init__(self, calls: int, period: float):
        self.calls = calls
        self.period = period
        self.timestamps = []
        self.lock = threading.Lock()

    def wait(self):
        # 注意：sleep 必须在锁外。否则持锁睡眠会把所有调用方串成一条不公平的队列
        # （突发时一个等待者持锁 sleep，其余全堵在 with self.lock 上、轮不到也不公平）。
        while True:
            with self.lock:
                now = time.time()
                self.timestamps = [t for t in self.timestamps if now - t < self.period]
                if len(self.timestamps) < self.calls:
                    self.timestamps.append(now)
                    return
                wait_time = self.timestamps[0] + self.period - now
            if wait_time > 0:
                time.sleep(wait_time)


class _PreemptiveQuotaExhausted(Exception):
    """LbQuotaTracker 检测到当月配额已耗尽时的内部哨兵异常。

    不抛 OpenApiException 是因为它的真实 ABI 是 4 参 (kind, code, trace_id, message)
    构造极易因 SDK 升级 ABI 不兼容；用内部异常类彻底解耦。
    上层 _fetch_segment_data 的 except 通过 isinstance 识别，code 属性给一致接口。
    """
    code = 301607
    message = "history kline symbol count out of limit (preemptive)"


def _quota_exhausted_advice(symbol) -> str:
    """按 LB symbol 的市场后缀返回可用的数据源建议。"""
    if not isinstance(symbol, str) or not symbol:
        return "考虑等待月初配额重置，或切到非长桥数据源"
    upper = symbol.upper()
    if upper.endswith(".HK"):
        return "港股建议切回通达信: 设 config.EXCHANGE_HK = 'tdx_hk'"
    if upper.endswith(".US"):
        return "美股建议切到盈立: 设 config.EXCHANGE_US = 'usmart' 且 US_HISTORY_KLINE_SOURCE = 'usmart'"
    if upper.endswith((".SH", ".SZ", ".BJ")):
        return "A 股建议切到通达信: 设 config.EXCHANGE_A = 'tdx'"
    return "考虑等待月初配额重置，或切到非长桥数据源"


def is_retryable_exception(exception):
    """
    判断异常是否需要重试
    """
    # 内部哨兵：本进程已知配额耗尽，retry 不会让配额变多，直接 break
    if isinstance(exception, _PreemptiveQuotaExhausted):
        return False
    if isinstance(exception, OpenApiException):
        # 301600: out of minute kline begin date - 数据超限，不重试
        # 301607: history kline symbol count out of limit - 配额耗尽，retry 也是浪费 6s
        code = getattr(exception, 'code', None)
        if code in (301600, 301607):
            return False
    return True


def time_logger(func):
    """
    用于监控函数执行耗时的装饰器
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        start_ts = time.time()
        # LogUtil.info(f"===> Start: {func.__name__}") # 可根据需要开启
        result = func(*args, **kwargs)
        end_ts = time.time()
        duration = end_ts - start_ts
        LogUtil.debug(f"<=== Finish: {func.__name__}, Duration: {duration:.4f}s")
        return result

    return wrapper


@fun.singleton
class ExchangeChangQiao(Exchange):
    """
    长桥交易所实现 - 高性能并发优化版
    """

    def __init__(self):
        # 这里只构建配置对象，不在构造时立刻连网。
        # 真正访问长桥时再懒加载 QuoteContext / TradeContext，避免应用启动阶段因网络问题直接失败。
        self.config = _build_longbridge_config()
        self._quote_context = None
        self._quote_context_lock = threading.Lock()  # 保护 _quote_context 引用的读写
        self._trade_context = None
        # 历史 bug：原来用单一字段 stock_list_cache 缓存 all_stocks 结果，且 all_stocks 写死
        # 只查 Market.US 的列表。配合 @fun.singleton（A/HK/US 共享同一实例），结果是任何
        # market 调 all_stocks 都返回同一份美股数据，导致 web 搜索框 A 股/港股全是美股标的。
        # 改为按 market 维度分桶缓存，并支持显式传入 market 区分。
        self.stock_list_cache: Dict[str, List[Dict]] = {}

        # 线程池配置
        # 建议设置在 8-16 之间。过高可能导致 API 触发流控限制 (Rate Limit)
        self.executor = ThreadPoolExecutor(max_workers=16)
        # _quote_call 专用线程池，与上面批量 K 线分段用的 self.executor 物理隔离。
        # 关键：klines() 把 N 个分段任务 submit 到 self.executor，每个分段内部又经
        # _fetch_candlesticks_api → _quote_call 再 submit；若共用一个有界池，分段填满
        # 16 个 worker 后内层 _quote_call 抢不到 worker → 分段阻塞在 future.result 上
        # → 8s 超时风暴(经典有界池嵌套提交死锁)。独立池让内层调用永远有 worker 可用。
        self._quote_call_executor = ThreadPoolExecutor(
            max_workers=16, thread_name_prefix="lb-qcall"
        )
        # 限流器。长桥行情接口默认配额约 10 QPS。交互请求(tv_history)与 prewarm/批量用两把
        # 独立令牌桶：交互默认 6 QPS、prewarm 默认 2 QPS(两者独立、峰值合计 8<10)，这样 prewarm
        # 突发不会饿死用户的增量轮询。均可用环境变量覆盖；嫌慢且账号配额够可调大
        # CHANLUN_LB_QUOTE_QPS，触发 301606 限流码则调小。
        self.rate_limiter = RateLimiter(
            calls=_env_int("CHANLUN_LB_QUOTE_QPS", 6), period=1.0
        )
        self.prewarm_rate_limiter = RateLimiter(
            calls=_env_int("CHANLUN_LB_PREWARM_QPS", 2), period=1.0
        )

        # 进程退出时显式关闭线程池，避免长桥 SDK 的 quote/trade 网络上下文
        # 在 daemon 线程里被强行打断造成日志噪音/连接半关。
        # 用 weakref 避免闭包持有 self 阻碍单例 GC。
        _self_ref = weakref.ref(self)

        def _shutdown_on_exit():
            inst = _self_ref()
            if inst is None:
                return
            try:
                inst.executor.shutdown(wait=False, cancel_futures=True)
                inst._quote_call_executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                # atexit 钩子内进程已在退出，logger 可能也已关闭；任何异常都不再传播。
                pass

        atexit.register(_shutdown_on_exit)

        # 启动后台心跳探针：每 30s 发一次 trading_session() 探活，
        # 发现卡死（超过 5s 不返回）时主动重建 QuoteContext，
        # 避免等到业务调用才发现连接已死。
        self._start_quote_ctx_watchdog()

    # ------------------------------------------------------------------
    # QuoteContext 生命周期管理
    # ------------------------------------------------------------------
    # 背景：长桥 SDK 底层（Rust wsclient）在 WS 断开时会立即取消所有
    # in-flight 请求，但上层 Core::run() 的重连循环期间不会通知
    # command_rx 队列里已排队的命令失败——导致 quote()/trading_session()
    # 等同步调用永久阻塞，直到进程重启。Config 无任何超时参数可配置。
    # 缓解策略：用外层 TimeoutError 检测阻塞，检测到后主动重建 QuoteContext。
    # 重建时 del old 触发 Rust Drop → command_rx 关闭 → Core::run() 退出。
    # ------------------------------------------------------------------

    _QUOTE_CALL_TIMEOUT = 8.0  # 秒；比 now_trading/ticks 的 2s 宽松，给 K 线接口留余量

    # 重建退避：ticks/now_trading/klines/watchdog 可能在 1~8s 内几乎同时超时。若每次
    # 超时都重建，会反复新建 QuoteContext——而旧 ctx 还被卡住的 in-flight 调用(worker
    # 线程)持有，引用计数>0、del 不触发 Rust Drop，旧 ws 连接泄漏累积，可能触发长桥连接
    # 数限制形成恶性循环(越重建越连不上)。故两次重建至少间隔 _REBUILD_MIN_INTERVAL 秒；
    # 冷却窗口内的并发超时只让第一个线程真正重建，其余直接降级返回(不重建)。
    _REBUILD_MIN_INTERVAL = 30.0  # 秒
    _last_rebuild_ts = None  # 上次重建的 time.monotonic()；类属性默认，实例首次重建时写

    def _quote_ctx(self) -> QuoteContext:
        """懒加载 QuoteContext 并返回当前实例（加锁保护引用读写）。"""
        with self._quote_context_lock:
            if self._quote_context is None:
                self._quote_context = QuoteContext(self.config)
            return self._quote_context

    def _rebuild_quote_ctx(self) -> None:
        """重建 QuoteContext。在检测到调用永久阻塞（超时）后调用。

        del old 触发 Rust 侧 Drop：command_rx 发送端关闭
        → Core::run() 检测到 recv() 返回 None → 任务正常退出。
        旧线程池线程若仍在 Rust 层阻塞，最多再等 REQUEST_TIMEOUT(30s) 后释放。
        """
        old_executor = None
        with self._quote_context_lock:
            now = time.monotonic()
            last = self._last_rebuild_ts
            if last is not None and (now - last) < self._REBUILD_MIN_INTERVAL:
                # 退避：距上次重建不足 _REBUILD_MIN_INTERVAL，跳过本次，避免重建风暴。
                # 并发超时只让第一个线程真正重建，其余落到这里直接返回（调用方降级）。
                return
            old = self._quote_context
            old_executor = self._quote_call_executor
            self._quote_context = QuoteContext(self.config)
            # 同时换掉 _quote_call 专用池：真正卡死(永不返回)的 longbridge 调用会占住该池
            # 的 worker 泄漏，换新池让后续调用拿到干净 worker，不被僵尸占满。
            self._quote_call_executor = ThreadPoolExecutor(
                max_workers=16, thread_name_prefix="lb-qcall"
            )
            self._last_rebuild_ts = now
            del old
        # 锁外 shutdown 旧池(不 wait，里面可能有卡死线程；cancel 掉还没开始的排队任务)
        if old_executor is not None:
            try:
                old_executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
        LogUtil.warning("[lb] QuoteContext + quote-call executor rebuilt due to connection hang")

    def _quote_call(self, fn, timeout: float = None):
        """在线程池里执行 fn()，超时则重建 QuoteContext 并重新抛出异常。

        用法：
            result = self._quote_call(lambda: self._quote_ctx().quote(symbols))

        注意：fn 内部通过 self._quote_ctx() 取引用（锁外执行），
        超时后主线程调 _rebuild_quote_ctx() 可安全拿到锁，不会死锁。
        """
        t = timeout if timeout is not None else self._QUOTE_CALL_TIMEOUT
        # 用专用池 _quote_call_executor，绝不能用批量 self.executor：klines() 把分段任务
        # submit 到 self.executor，分段内部又经 _fetch_candlesticks_api → 本函数再 submit，
        # 共用同一有界池会嵌套死锁(分段占满 worker、内层抢不到)。详见 __init__ 注释。
        future = self._quote_call_executor.submit(fn)
        try:
            return future.result(timeout=t)
        except (FuturesTimeoutError, TimeoutError):
            # 关键：future.result(timeout=) 抛的是 concurrent.futures.TimeoutError，
            # Python<3.11 它不是 builtin TimeoutError(OSError 子类)。两个都列上，
            # 否则超时捕获不到 → _rebuild_quote_ctx() 永不执行、连接半死无法自愈。
            LogUtil.error(
                f"[lb] QuoteContext call timed out after {t}s, rebuilding ctx"
            )
            self._rebuild_quote_ctx()
            raise

    def _start_quote_ctx_watchdog(self, interval: float = 30.0) -> None:
        """启动后台守护线程，定期用 trading_session() 探活 QuoteContext。

        trading_session() 无 symbol 参数，不触发 per-symbol 速率限制(301606)。
        30s 间隔远小于 SDK 心跳死亡判定阈值(120s)，可在业务调用前主动发现并重建。
        """
        _self_ref = weakref.ref(self)

        def _watchdog():
            while True:
                time.sleep(interval)
                inst = _self_ref()
                if inst is None:
                    return  # 实例已被 GC，退出守护线程
                if inst._quote_context is None:
                    continue  # 尚未初始化，跳过本轮
                try:
                    inst._quote_call(
                        lambda: inst._quote_ctx().trading_session(),
                        timeout=5.0,
                    )
                except (FuturesTimeoutError, TimeoutError):
                    pass  # _quote_call 内已重建并记日志
                except Exception:
                    pass  # 其他偶发错误（如 OpenApiException）不影响守护循环

        t = threading.Thread(
            target=_watchdog,
            daemon=True,
            name="lb-quote-watchdog",
        )
        t.start()

    def _trade_ctx(self) -> TradeContext:
        # TradeContext 同样使用懒加载，避免只查行情时也初始化交易连接。
        if self._trade_context is None:
            self._trade_context = TradeContext(self.config)
        return self._trade_context

    def default_code(self) -> str:
        return "TSLA.US"

    def support_frequencys(self) -> dict:
        return {
            "1m": "1 Min",
            "5m": "5 Min",
            "15m": "15 Min",
            "30m": "30 Min",
            "60m": "60 Min",
            "d": "Day",
            "w": "Week",
            "m": "Month",
        }

    # 长桥 OpenAPI 实测结论（2026-04 验证）：
    #   - SecurityListCategory 枚举只有 Overnight 一个值；
    #   - security_list 仅 (Market.US, Overnight) 组合可用，其它市场 / 不传 category 都返回 param_error；
    #   - QuoteContext 没有任何"按市场拉全量列表"的能力（watchlist 是用户自选股、static_info 必须先知道 code）。
    # 因此港股 / A 股的全量列表只能从外部数据源拿。这里采用 lazy fallback：
    #   - hk → 复用 ExchangeTDXHK.all_stocks()（通达信港股）
    #   - a  → 复用 ExchangeTDX.all_stocks()（通达信 A 股）
    # K 线 / Tick / 下单等其它接口仍然完全走长桥，不受影响。
    _LB_MARKET_MAP = {
        "us": Market.US,
    }

    # static_info 单批上限：实测 500 OK，留点余量取 500（长桥未公开硬上限，但 500 已稳定）。
    _STATIC_INFO_BATCH_SIZE = 500

    # 项目内部 code 与长桥 symbol 不是同一种格式：
    #   美股: 项目 "TSLA.US"        ↔ 长桥 "TSLA.US"          （一致, 不用转换）
    #   港股: 项目 "KH.00700"       ↔ 长桥 "00700.HK"         （前缀↔后缀互换）
    #   A 股: 项目 "SH.600519"      ↔ 长桥 "600519.SH"        （前缀↔后缀互换）
    # 长桥 OpenAPI 一律是 "<code>.<exchange>"; 项目历史代码里港股/A 股是 "<market_prefix>.<code>"。
    # 之前 klines/ticks/stock_info 直接把项目 code 透传给长桥, 港股/A 股就一直拿不到数据。
    _PREFIX_TO_LB_SUFFIX = {
        "KH": "HK",   # 港股
        "SH": "SH",   # 沪市 A 股
        "SZ": "SZ",   # 深市 A 股
    }
    _LB_SUFFIX_TO_PREFIX = {v: k for k, v in _PREFIX_TO_LB_SUFFIX.items()}

    @classmethod
    def _to_lb_symbol(cls, code: str) -> str:
        """把现行项目代码转换为长桥 symbol，并拒绝未知格式。"""
        if type(code) is not str or not code or "." not in code:
            raise ValueError("invalid Longbridge symbol")
        head, tail = code.split(".", 1)
        if head in cls._PREFIX_TO_LB_SUFFIX:
            if not tail:
                raise ValueError("invalid Longbridge symbol")
            return f"{tail}.{cls._PREFIX_TO_LB_SUFFIX[head]}"
        suffix = code.rsplit(".", 1)[1].upper()
        if suffix in {"US", "HK", "SH", "SZ"}:
            return code
        raise ValueError("unsupported Longbridge symbol market")

    @classmethod
    def _market_of_code(cls, code: str) -> str:
        """从项目内部 code 派生市场标识，用于精度归一等需要 market 的场合。

        格式约定（见 _PREFIX_TO_LB_SUFFIX 注释）：
          A 股  SH.600519 / SZ.000001  → "a"
          港股  KH.00700               → "hk"
          美股  TSLA.US                → "us"
        """
        if type(code) is not str or not code or "." not in code:
            raise ValueError("invalid project symbol")
        head = code.split(".", 1)[0].upper()
        if head in ("SH", "SZ"):
            return "a"
        if head == "KH":
            return "hk"
        suffix = code.rsplit(".", 1)[1].upper()
        if suffix in ("SH", "SZ"):
            return "a"
        if suffix == "HK":
            return "hk"
        if suffix == "US":
            return "us"
        raise ValueError("unsupported project symbol market")

    @classmethod
    def _from_lb_symbol(cls, symbol: str) -> str:
        """长桥 symbol → 项目内部 code。美股保持 TSLA.US 不变。"""
        if not symbol or "." not in symbol:
            return symbol
        head, tail = symbol.split(".", 1)
        # 美股不需要反转
        if tail not in cls._LB_SUFFIX_TO_PREFIX or tail == "US":
            return symbol
        return f"{cls._LB_SUFFIX_TO_PREFIX[tail]}.{head}"

    def _enrich_us_names_with_static_info(self, stocks: List[Dict]) -> List[Dict]:
        """用长桥 ``static_info`` 批量补全美股的中文名（``name_cn``）。

        背景：``security_list`` 返回的 ``name_cn`` 经常为空，会 fallback 到 ``name_en``，
        导致前端"标的列表"全是英文。``static_info`` 的同一字段几乎总有中文（实测 100%
        命中率），且支持批量（500/批），11755 条总耗时 1~2s。

        策略：
        - 分批 ``_STATIC_INFO_BATCH_SIZE``，每批失败不影响其它批（降级保留原英文 name）。
        - 优先 ``name_cn``，其次 ``name_en``，再次保留原 ``name``。
        - 整体异常仍返回原列表，保证 ``all_stocks`` 不会因为补全失败而变空。
        """
        if not stocks:
            return stocks

        # 用 dict 把 code → name 重映射回去，避免 O(n*m) 查找
        code_to_name: Dict[str, str] = {}
        codes = [s["code"] for s in stocks if s.get("code")]
        batch_size = self._STATIC_INFO_BATCH_SIZE
        total_batches = (len(codes) + batch_size - 1) // batch_size

        t0 = time.time()
        ok_batches = 0
        for i in range(0, len(codes), batch_size):
            batch = codes[i:i + batch_size]
            try:
                # 用默认参数固定当前批次的 batch（避免 lambda 延迟捕获循环变量）。
                # 每次通过 self._quote_ctx() 取最新实例，避免重建后仍用旧引用。
                infos = self._quote_call(lambda b=batch: self._quote_ctx().static_info(b))
                for info in infos:
                    sym = getattr(info, "symbol", None)
                    if not sym:
                        continue
                    cn = (getattr(info, "name_cn", "") or "").strip()
                    en = (getattr(info, "name_en", "") or "").strip()
                    code_to_name[sym] = cn or en or code_to_name.get(sym, "")
                ok_batches += 1
            except Exception as e:
                # 单批失败仅记录，继续下一批；该批的标的保留原英文名。
                LogUtil.info(
                    f"_enrich_us_names: batch {i // batch_size + 1}/{total_batches} "
                    f"failed (size={len(batch)}): {e}"
                )

        # 合并回原列表，没匹配上的保留原 name（英文兜底）。
        enriched = []
        cn_count = 0
        for s in stocks:
            new_name = code_to_name.get(s["code"], s.get("name", ""))
            if new_name and new_name != s.get("name"):
                cn_count += 1
            enriched.append({"code": s["code"], "name": new_name})

        LogUtil.info(
            f"_enrich_us_names: total={len(stocks)} batches_ok={ok_batches}/{total_batches} "
            f"updated={cn_count} elapsed={time.time() - t0:.2f}s"
        )
        return enriched

    def _fallback_all_stocks(self, market_key: str) -> List[Dict]:
        """当长桥 SDK 不支持时，借用项目里其它 exchange 实现拿全量列表。

        ``ExchangeTDXHK`` / ``ExchangeTDX`` 在内部都有自己的缓存（``g_all_stocks``）和
        服务器选择逻辑，这里 lazy import 避免引入循环依赖、也避免在长桥单进程启动时
        无谓地建立通达信连接。

        失败容错：检测 ``init_failed`` 标志, 通达信连接坏了就快速返回空, 不再卡 30s。
        """
        try:
            if market_key == "hk":
                from chanlun.exchange.exchange_tdx_hk import ExchangeTDXHK
                ex = ExchangeTDXHK()
                if getattr(ex, "init_failed", False):
                    LogUtil.warning(
                        "all_stocks fallback: ExchangeTDXHK init_failed, return []"
                    )
                    return []
                return ex.all_stocks() or []
            if market_key == "a":
                from chanlun.exchange.exchange_tdx import ExchangeTDX
                ex = ExchangeTDX()
                if getattr(ex, "init_failed", False):
                    LogUtil.warning(
                        "all_stocks fallback: ExchangeTDX init_failed, return []"
                    )
                    return []
                return ex.all_stocks() or []
        except Exception as e:
            LogUtil.error(
                f"all_stocks fallback failed: market={market_key!r}, err={e}"
            )
        return []

    def all_stocks(self, market: str):
        """获取指定 market 的全量 symbol 列表。

        :param market: 项目内的 market 标识（"a"/"hk"/"us"）。

        缓存策略：按 market 分桶，避免互相污染。
        """
        if type(market) is not str or market.lower() not in {"a", "hk", "us"}:
            raise ValueError("unsupported Longbridge stock-list market")
        market_key = market.lower()

        cached = self.stock_list_cache.get(market_key)
        if cached is not None:
            return cached

        try:
            stocks: List[Dict] = []
            lb_market = self._LB_MARKET_MAP.get(market_key)
            if lb_market is not None:
                # 美股：长桥唯一支持 security_list 的市场，保留原 Overnight 行为。
                resp = self._quote_call(
                    lambda: self._quote_ctx().security_list(
                        lb_market, SecurityListCategory.Overnight
                    )
                )
                stocks = [
                    {"code": info.symbol, "name": getattr(info, "name_cn", None) or info.name_en}
                    for info in resp
                ]
                # security_list 的 name_cn 大概率为空，用 static_info 批量补全中文名。
                # 实测 11755 条 ÷ 500/批 ≈ 24 批，1~2 秒可全量补完，name_cn 命中率 100%。
                stocks = self._enrich_us_names_with_static_info(stocks)
            else:
                # 港股 / A 股 / 外汇：长桥不提供全量列表，借助通达信 exchange 拿。
                stocks = self._fallback_all_stocks(market_key)
                LogUtil.info(
                    f"all_stocks: market={market_key!r} 通过 fallback 获取到 {len(stocks)} 条"
                )

            self.stock_list_cache[market_key] = stocks
            return stocks
        except Exception as e:
            LogUtil.error(f"Error in all_stocks(market={market_key!r}): {e}")
            # 出错时也尝试 fallback，避免单点失败导致搜索完全不可用。
            if market_key not in self._LB_MARKET_MAP:
                return self._fallback_all_stocks(market_key)
            return []

    def now_trading(self, market: str):
        """按市场对应的交易时段和本地时区判断当前是否开市。"""
        if pytz is None:
            return False

        _market_map = {
            "us": (Market.US, "America/New_York"),
            "hk": (Market.HK, "Asia/Hong_Kong"),
        }
        if market not in _market_map:
            raise ValueError("unsupported Longbridge trading-session market")
        lb_market, tz_name = _market_map[market]

        def _do_check():
            try:
                local_tz = pytz.timezone(tz_name)
                now_local = datetime.now(local_tz).time()
                sessions = self._quote_ctx().trading_session()

                for session in sessions:
                    if session.market == lb_market:
                        for trade_info in session.trade_sessions:
                            try:
                                start_time = trade_info.begin_time
                                end_time = trade_info.end_time

                                if not isinstance(start_time, datetime_time) or not isinstance(end_time, datetime_time):
                                    continue

                                if start_time > end_time:
                                    if now_local >= start_time or now_local < end_time:
                                        return True
                                else:
                                    if start_time <= now_local and now_local < end_time:
                                        return True
                            except AttributeError:
                                continue
                        return False
                return False
            except Exception as e:
                LogUtil.warning(f"Error checking trading session: {e}")
                return False

        # 用 _quote_call 提交并设 2s 超时，检测到卡死时自动重建 QuoteContext
        try:
            return self._quote_call(_do_check, timeout=2.0)
        except Exception as e:
            LogUtil.error(f"now_trading timeout or error: {e}")
            return False

    def _fetch_candlesticks_api(self, symbol, period, adjust_type, count, time_cursor, trade_sessions,
                                priority="interactive"):
        """
        带限流和重试的 history kline API 调用。

        按优先级分档快/慢失败:
          - interactive(切标的/首屏/SSE/轮询): 超时 5s、重试 2 次、短退避(1~3s)。长桥 API
            卡顿时最坏 ~11s 即放弃(原 8s×3次 + 2~10s 退避 可达 20~30s), 避免用户切标的傻等。
            代价: API 真慢时该标的可能拉不全(由上层缓存/旧数据兜底)。
          - prewarm/批量: 保留 8s 超时、3 次重试、长退避(2~10s), 后台慢可接受、尽量拉全。

        前置短路：当月配额已耗尽且 symbol 不在本月已查过的集合里 → 抛 _PreemptiveQuotaExhausted
        让上层 _fetch_segment_data 走"已知失败"分支，避免无意义的 SDK 调用浪费 retry 预算。
        用内部哨兵异常而非 OpenApiException 是为了避开 SDK 4 参 ABI（kind, code, trace_id, message）。
        reraise=True: 重试耗尽抛原异常(非 RetryError), 与 _fetch_segment_data 的 err_code 解包一致。
        """
        if priority == "interactive":
            retryer = Retrying(
                stop=stop_after_attempt(2),
                wait=wait_exponential(multiplier=1, min=1, max=3),
                retry=retry_if_exception(is_retryable_exception),
                reraise=True,
            )
            call_timeout = 5.0
        else:
            retryer = Retrying(
                stop=stop_after_attempt(3),
                wait=wait_exponential(multiplier=1, min=2, max=10),
                retry=retry_if_exception(is_retryable_exception),
                reraise=True,
            )
            call_timeout = self._QUOTE_CALL_TIMEOUT

        def _do_call():
            tracker = LbQuotaTracker.instance()
            limit = getattr(config, "LB_QUOTA_MONTHLY_LIMIT", 0)
            if limit <= 0 and not getattr(self, "_quota_limit_zero_warned", False):
                LogUtil.warning(
                    "[lb_quota] LB_QUOTA_MONTHLY_LIMIT not configured (=0), preemptive "
                    "short-circuit disabled; only reactive mark_exhausted() will work"
                )
                self._quota_limit_zero_warned = True
            if tracker.is_exhausted(limit) and not tracker.has_symbol(symbol):
                LogUtil.debug(
                    f"[lb_quota] preemptive short-circuit symbol={symbol} "
                    f"count={tracker.count()} limit={limit}"
                )
                raise _PreemptiveQuotaExhausted()

            # prewarm/批量走独立低优先令牌桶，不与交互请求争抢同一个 3~6 QPS 额度
            limiter = self.prewarm_rate_limiter if priority == "prewarm" else self.rate_limiter
            limiter.wait()
            result = self._quote_call(
                lambda: self._quote_ctx().history_candlesticks_by_offset(
                    symbol=symbol,
                    period=period,
                    adjust_type=adjust_type,
                    forward=False,  # 向前追溯
                    count=count,
                    time=time_cursor,
                    trade_sessions=trade_sessions
                ),
                timeout=call_timeout,
            )
            # 记录本次成功的 symbol 进入本月配额集合
            tracker.add_symbol(symbol)
            return result

        return retryer(_do_call)

    def _fetch_segment_data(self, code, period, adjust, end_dt, start_dt_limit, priority="interactive"):
        """
        [内部并发任务] 获取特定时间片段的数据
        策略：从 end_dt 向前获取，直到遇到 start_dt_limit 或数据耗尽
        """
        segment_candles = []
        current_cursor = end_dt
        # 三态:complete=正常结束/空返回(数据耗尽); reached_origin=301600 合法到历史起点;
        # failed=真失败(API 异常/超时/配额/时间解析错误)→该段不可信,可能带洞(C1)。
        status = "complete"

        # 确保 limit 是带时区的
        tz = pytz.timezone("Asia/Shanghai")
        if start_dt_limit.tzinfo is None:
            start_dt_limit = tz.localize(start_dt_limit)

        while True:
            try:
                # 核心 API 调用
                candlesticks = self._fetch_candlesticks_api(
                    symbol=code,
                    period=period,
                    adjust_type=adjust,
                    count=1000,
                    time_cursor=current_cursor,
                    trade_sessions=TradeSessions.Intraday,
                    priority=priority
                )
            except Exception as e:
                # tenacity 重试失败或遇到不可重试异常
                # 解包 RetryError 拿底层 OpenApiException 真实 code
                inner = e
                if hasattr(e, 'last_attempt'):
                    try:
                        inner = e.last_attempt.exception() or e
                    except Exception:
                        pass
                err_code = getattr(inner, 'code', None)
                if isinstance(inner, OpenApiException) and err_code == 301600:
                    LogUtil.warning(f"Data limit reached (301600) for {code}, stopping segment.")
                    status = "reached_origin"   # 合法到该 symbol 历史起点,非洞
                elif (isinstance(inner, _PreemptiveQuotaExhausted) or
                      (isinstance(inner, OpenApiException) and err_code == 301607)):
                    # 月度 symbol 配额耗尽：标记 exhausted 让后续调用立刻短路
                    LbQuotaTracker.instance().mark_exhausted()
                    LogUtil.error(
                        f"Monthly quota exhausted (301607) for {code}: "
                        f"{getattr(inner, 'message', None) or str(inner)[:200]}. "
                        f"{_quota_exhausted_advice(code)}"
                    )
                    status = "failed"           # 配额耗尽→该段不可信
                else:
                    LogUtil.error(
                        f"API Retry failed segment={end_dt} code={code} period={period} "
                        f"err_type={type(inner).__name__} err_code={err_code} "
                        f"err_msg={getattr(inner, 'message', None) or str(inner)[:200]} "
                        f"outer={type(e).__name__}"
                    )
                    status = "failed"           # 真失败→该段不可信,可能带洞
                break

            if not candlesticks:
                break

            # 获取这批数据中最老的一条时间，用于更新游标
            oldest_candle = candlesticks[0]

            # --- 统一转为带时区的 datetime 进行比较 ---
            # pd.to_datetime(标量) 返回 Timestamp，没有 .dt 访问器，使用 pd.Timestamp 构造。
            try:
                ts = oldest_candle.timestamp
                if isinstance(ts, (int, float)):
                    # Unix 时间戳 (秒) -> UTC -> 转上海
                    oldest_dt = pd.Timestamp(ts, unit='s', tz='UTC').tz_convert(tz).to_pydatetime()
                else:
                    # Datetime 对象
                    oldest_dt = pd.to_datetime(ts).to_pydatetime()
                    if oldest_dt.tzinfo is None:
                        # 如果是 Naive，假设它已经是上海时间，贴上标签
                        oldest_dt = tz.localize(oldest_dt)
                    else:
                        oldest_dt = oldest_dt.astimezone(tz)
            except Exception as e:
                LogUtil.error(f"Time parse error in segment fetch: {e}")
                status = "failed"
                break
            # -------------------------------------------

            segment_candles.extend(candlesticks)

            # 更新游标
            current_cursor = oldest_dt

            if len(candlesticks) < 1000:
                break

            if oldest_dt < start_dt_limit:
                break

        return segment_candles, status

    @time_logger  # 开启耗时监控
    def klines(
            self,
            code: str,
            frequency: str,
            start_date: str = None,
            end_date: str = None,
            args=None,
    ) -> pd.DataFrame:
        """
        获取 Kline 线 (并发优化版)
        """
        args = args or {}
        tz = pytz.timezone("Asia/Shanghai")

        # 长桥 API 需要 "<code>.<exchange>" 格式 (如 00700.HK / 600519.SH);
        # 项目内部港股/A 股 code 是 "<prefix>.<code>" (如 KH.00700 / SH.600519)。
        # 在请求边界一次性转换, 输出 DataFrame 的 code 列继续保留项目格式不变。
        lb_symbol = self._to_lb_symbol(code)

        # 默认回看周期从统一表读取，修改请改 _lookback.py 而非这里。
        from chanlun.exchange._lookback import get_lookback_timedelta

        # 2. 时间标准化处理
        # 标记：是否是“历史查询”模式（即用户是否指定了截止日期）
        is_history_query = end_date is not None

        now_dt = datetime.now(tz)

        if is_history_query:
            # 用户指定了结束时间，严格按照用户时间查询
            end_dt = str_to_datetime(end_date)
            if end_dt.tzinfo is None:
                end_dt = tz.localize(end_dt)
            else:
                end_dt = end_dt.astimezone(tz)
            # API 请求游标
            api_cursor_dt = end_dt
        else:
            # 用户查最新：end_dt 设为当前时间
            end_dt = now_dt
            # API 请求游标：往后推 5 分钟，确保能囊括服务器端刚刚生成的最新 K 线
            # 解决本地时间落后服务器几秒导致拿不到最新数据的问题
            api_cursor_dt = now_dt + timedelta(minutes=5)

        if start_date:
            start_dt = str_to_datetime(start_date)
            if start_dt.tzinfo is None:
                start_dt = tz.localize(start_dt)
            else:
                start_dt = start_dt.astimezone(tz)
        else:
            lookback = get_lookback_timedelta(frequency)
            start_dt = end_dt - lookback

        # 限制最大回看范围:不允许请求超过统一 lookback 表的历史数据
        allow_long_history = bool(
            args.get("allow_long_history") or args.get("no_lookback_limit")
        )
        if not allow_long_history:
            max_lookback = get_lookback_timedelta(frequency)
            # 裁剪基准用 end_dt(查询窗口的结束)而非 now_dt:历史区间查询(end_date 在过去)用
            # now_dt 会把 start 裁到"现在回看窗口"、导致 start>=end 直接返空(审查 H2)。改 end_dt
            # 后语义为"从 end_dt 往前最多 lookback",窗口宽度仍受限(不超拉),历史查询也成立。
            earliest_allowed = end_dt - max_lookback
            if start_dt < earliest_allowed:
                start_dt = earliest_allowed
        if start_dt >= end_dt:
            return pd.DataFrame()

        # 3. 周期映射
        period_map = {
            "1m": Period.Min_1, "5m": Period.Min_5, "15m": Period.Min_15,
            "30m": Period.Min_30, "60m": Period.Min_60, "d": Period.Day,
            "w": Period.Week, "m": Period.Month,
        }

        if frequency not in period_map:
            # cq(长桥)只支持 period_map 内周期(与 support_frequencys 一致)。其余周期(季 q/年 y/
            # 2m/3m/10m/120m 等)正常 UI 经 supported_resolutions 过滤不会暴露、不会调到此; 仅手构
            # 请求(绕过前端闸门)可达。历史此处静默递归拉 1m, 但对 q/y 等粗周期 = 把 1m 数据当该
            # 周期显示(数据语义错配)。改为明确返回空 DataFrame——长桥不支持的周期就如实拒绝, 不降级。
            LogUtil.warning(f"[cq] 不支持的周期 frequency={frequency} code={code}, 返回空(不降级 1m)")
            return pd.DataFrame()

        period = period_map[frequency]
        adjust = AdjustType.ForwardAdjust

        # 5. 并发分片策略
        # chunk_days 按"每段约装满单次 API 的 count=1000 根"重定,最小化"段边界拉不满 1000"的
        # 令牌浪费——QPS 受限时令牌(过 6 QPS 桶)是真瓶颈,多切一段=多费一个令牌(段越细令牌越多)。
        # 实测各周期 chunk 前→后令牌(4 标的 INTC/MSFT/AMZN/GOOGL 交叉验证, 1m lookback=20 天口径):
        #   60m 13→4(旧 60 天 13 段每段~250 根, 新 200 天 4 段每段~960 根, 省 9, 大头) / 30m 7→5 /
        #   1m 9→8(数据密, 10 天/段~2535 根翻 3 页, 段少边界省) / 5m 维持 15=7(超 17 天跨 1000 边界反增) / d 1。
        # 单标的 5 周期 37→25 令牌(省 32%), 墙钟随令牌数同比缩短。⚠数字依赖 1m lookback=20 天, 调整需重核。
        if frequency == '1m':
            chunk_days = 10
        elif frequency == '5m':
            chunk_days = 15
        elif frequency == '15m':
            chunk_days = 60
        elif frequency == '30m':
            chunk_days = 110
        elif frequency == '60m':
            chunk_days = 200
        else:
            chunk_days = 3650

        # 读取本次调用优先级(prewarm 由 lb_low_priority() 在 caller 线程上标记)，随分段透传，
        # 让 _fetch_candlesticks_api 选对应限流桶；分段跑在别的 worker 线程上拿不到 threadlocal，
        # 必须显式随 submit 传参。
        priority = getattr(_lb_call_priority, "value", "interactive")
        # tasks 携带每段窗口 (future, seg_start, seg_end),供失败段串行补拉与完整性闸门定位(C1)。
        tasks = []
        curr_end = api_cursor_dt  # 使用带 Buffer 的游标

        while curr_end > start_dt:
            curr_start = curr_end - timedelta(days=chunk_days)
            if curr_start < start_dt:
                curr_start = start_dt

            fut = self.executor.submit(
                self._fetch_segment_data,
                lb_symbol, period, adjust, curr_end, curr_start, priority
            )
            tasks.append((fut, curr_start, curr_end))
            curr_end = curr_start
            if curr_end <= start_dt:
                break

        # 6. 收集结果 + 源级完整性闸门(C1)
        all_candles = []
        failed = []  # [(seg_start, seg_end)] 真失败段(该段可能带洞)
        win = {f: (s, e) for f, s, e in tasks}
        for future in as_completed(win):
            s, e = win[future]
            try:
                res, seg_status = future.result()
                if res:
                    all_candles.extend(res)
                if seg_status == "failed":
                    failed.append((s, e))
            except Exception as ex:
                LogUtil.error(f"Kline segment fetch error win=[{s},{e}]: {ex}")
                failed.append((s, e))

        # 失败段串行补拉一次:恢复瞬间往往只是某段超时,二次多半成功。
        for (s, e) in list(failed):
            try:
                res, seg_status = self._fetch_segment_data(lb_symbol, period, adjust, e, s, priority)
                if seg_status != "failed":
                    if res:
                        all_candles.extend(res)
                    failed.remove((s, e))
            except Exception:
                pass

        # 完整性闸门:仍有失败段 → 本次拉取带洞不可信,返回空 DataFrame 且标 attrs['fetch_incomplete'],
        # 绝不把带洞结果当权威落盘(C1)。attrs 让 web 层区分「异常空→短退避 30s 自愈」与「真空→5min
        # 负缓存」(见 chart_compute/sse_refresh);回测/monitor 忽略 attrs → 视同现状"拉取失败返空",契约不变。
        if failed:
            LogUtil.error(f"[cq] {code} {frequency} 拉取不完整 failed={failed}, 拒绝返回带洞数据")
            _incomplete = pd.DataFrame()
            _incomplete.attrs["fetch_incomplete"] = True
            return _incomplete

        if not all_candles:
            return pd.DataFrame()

        # *** 7. 向量化构建 DataFrame (核心修复) ***
        try:
            # 同 timestamp 去重:实时进行中 bar 可能被多个分段各拉到一次(OHLC/volume 不同),
            # as_completed 完成顺序不确定 → 原 {ts:c} 字典推导保留"最后 extend 进来"的那个 = 末根
            # K 线不确定时，同一时间戳取成交量最大者；进行中 K 线的成交量单调增加，最大值即最新值。
            # 快照,结果确定。历史 bar 各 ts 唯一,行为不变。
            _by_ts = {}
            for c in all_candles:
                _prev = _by_ts.get(c.timestamp)
                if _prev is None or float(c.volume) >= float(_prev.volume):
                    _by_ts[c.timestamp] = c
            unique_candles = _by_ts.values()
            if not unique_candles:
                return pd.DataFrame()

            data = [
                (c.timestamp, float(c.open), float(c.high), float(c.low), float(c.close), float(c.volume))
                for c in unique_candles
            ]

            df = pd.DataFrame(data, columns=["date", "open", "high", "low", "close", "volume"])

            # === 时间处理逻辑 ===
            if len(data) > 0:
                first_ts = data[0][0]

                # 区分 Unix Timestamp 和 Datetime Object
                if isinstance(first_ts, (int, float)):
                    # Unix Timestamp: 必须指定 unit='s' 和 utc=True，然后转上海
                    df["date"] = pd.to_datetime(df["date"], unit='s', utc=True)
                    df["date"] = df["date"].dt.tz_convert("Asia/Shanghai")
                else:
                    # 已经是 Datetime 对象
                    df["date"] = pd.to_datetime(df["date"])

                    if df["date"].dt.tz is None:
                        # Naive 时间数值已是本地时间，用 tz_localize 贴标签而非转换数值。
                        df["date"] = df["date"].dt.tz_localize("Asia/Shanghai")
                    else:
                        # 如果自带时区，则转换为上海时间
                        df["date"] = df["date"].dt.tz_convert("Asia/Shanghai")
            # ==========================

            # 下界过滤 >= start_dt；上界仅历史查询时限制 <= end_dt，
            # 查实时增量时不过滤，避免本地时钟微小差异丢掉最新 K 线。
            mask = df["date"] >= start_dt
            if is_history_query:
                mask = mask & (df["date"] <= end_dt)

            df = df.loc[mask]

            # 补充字段并排序
            df["code"] = code
            df["frequency"] = frequency
            df = df.sort_values(by="date").reset_index(drop=True)

            df = df[["date", "frequency", "code", "open", "high", "low", "close", "volume"]]
            market = self._market_of_code(code)
            df = normalize_kline_precision(df, market, code)
            quantum = resolve_structure_price_quantum(market, code)
            if quantum is not None:
                metadata = build_provider_price_basis_metadata(
                    provider="longbridge",
                    market=market,
                    code=code,
                    adjustment="forward",
                    structure_price_quantum=quantum,
                )
                df = attach_price_basis_metadata(df, metadata)
            return df

        except Exception as e:
            LogUtil.error(f"DataFrame construction error: {e}")
            return pd.DataFrame()

    def ticks(self, codes: List[str]) -> Dict[str, Tick]:
        """
        获取 Ticks
        """
        def _do_ticks():
            try:
                # 项目 code → 长桥 symbol (港股/A 股要换前后缀; 美股原样)。
                # 同时建反向表, 把长桥返回的 symbol 映射回项目 code, 让调用方拿到的
                # dict key 与传入 codes 一致 (上层经常用 ticks[code] 直接取)。
                lb_symbols = [self._to_lb_symbol(c) for c in codes]
                lb_to_project = {lb: orig for lb, orig in zip(lb_symbols, codes)}

                # 直接调用：外层 _quote_call(_do_ticks, timeout=2.0) 已统一负责超时
                # 检测与重建，这里再套一层 _quote_call 会多占一个 16-worker 线程池名额
                # （且外 2s / 内 8s 双超时无实际意义）。由外层兜底即可。
                quotes = self._quote_ctx().quote(lb_symbols)
                res = {}
                for q in quotes:
                    last_done = float(q.last_done)
                    prev_close = float(q.prev_close)

                    cal_rate = 0.0
                    if prev_close > 0:
                        cal_rate = round(((last_done - prev_close) / prev_close) * 100, 2)

                    project_code = lb_to_project.get(q.symbol, self._from_lb_symbol(q.symbol))
                    res[project_code] = Tick(
                        code=project_code,
                        last=last_done,
                        buy1=0.0,
                        sell1=0.0,
                        high=float(q.high),
                        low=float(q.low),
                        open=float(q.open),
                        volume=float(q.volume),
                        rate=cal_rate
                    )
                return res
            except Exception as e:
                LogUtil.error(f"Error in ticks: {e}")
                return {}

        # 用 _quote_call 提交并设 2s 超时，检测到卡死时自动重建 QuoteContext
        try:
            return self._quote_call(_do_ticks, timeout=2.0)
        except Exception as e:
            LogUtil.error(f"ticks timeout or error: {e}")
            return {}

    def stock_info(self, code: str) -> Union[Dict, None]:
        """
        获取股票基础信息
        """
        try:
            # 所有市场统一通过 Longbridge symbol 转换，避免市场后缀污染。
            symbol = self._to_lb_symbol(code)
            infos = self._quote_call(lambda: self._quote_ctx().static_info([symbol]))
            if not infos:
                return None
            info = infos[0]
            return {
                # 对外仍然返回项目 code (KH.00700), 避免污染上层缓存键。
                "code": self._from_lb_symbol(info.symbol),
                "name": info.name_cn,
                "exchange": info.exchange,
                "currency": info.currency,
                "lot_size": info.lot_size,
                "total_shares": info.total_shares,
                "circulating_shares": info.circulating_shares,
            }
        except Exception as e:
            LogUtil.error(f"Error in stock_info: {e}")
            return None

    def stock_owner_plate(self, code: str):
        raise Exception("交易所不支持")

    def plate_stocks(self, code: str):
        raise Exception("交易所不支持")

    def balance(self):
        """
        获取账户余额
        """
        try:
            balances = self._trade_ctx().account_balance()
            if not balances:
                return {}
            b = balances[0]
            return {
                "total_cash": float(b.total_cash),
                "max_finance_amount": float(b.max_finance_amount),
                "currency": b.currency,
            }
        except Exception as e:
            LogUtil.error(f"Error in balance: {e}")
            return {}

    def positions(self, code: str = ""):
        """
        获取持仓
        """
        try:
            resp = self._trade_ctx().stock_positions()
            positions = []
            for channel in resp.channels:
                for p in channel.positions:
                    pos = {
                        "code": p.symbol,
                        "qty": p.quantity,
                        "can_sell_qty": p.available_quantity,
                        "cost_price": float(p.cost_price),
                    }
                    positions.append(pos)
            if code:
                positions = [p for p in positions if p["code"] == code]
            return positions
        except Exception as e:
            LogUtil.error(f"Error in positions: {e}")
            raise RuntimeError("Longbridge position query failed") from e

    def order(self, code: str, o_type: str, amount: float, args=None):
        """
        下单
        """
        side = OrderSide.Buy if o_type.lower() == "buy" else OrderSide.Sell
        order_type = OrderType.MO
        price = None

        if args and "price" in args:
            price = Decimal(str(args["price"]))
            order_type = OrderType.LO

        qty = Decimal(str(amount))

        try:
            resp = self._trade_ctx().submit_order(
                symbol=code,
                order_type=order_type,
                side=side,
                submitted_quantity=qty,
                time_in_force=TimeInForceType.Day,
                submitted_price=price,
            )
            return resp.order_id
        except Exception as e:
            LogUtil.error(f"Error in order: {e}")
            return None


class ExchangeChangQiaoMarketView:
    """把共享长桥连接绑定到不可变市场，避免 HK/US 互相覆盖默认市场。"""

    __slots__ = ("_backend", "_market")

    def __init__(self, backend, market: str):
        self._backend = backend
        self._market = str(market).lower()

    @property
    def market(self) -> str:
        return self._market

    def all_stocks(self):
        return self._backend.all_stocks(self.market)

    def now_trading(self, market: str):
        if market != self.market:
            raise ValueError("market view received a mismatched market")
        return self._backend.now_trading(market)

    def __getattr__(self, name):
        return getattr(self._backend, name)
