import datetime
import hashlib
import math
import os
import tempfile
import threading
from collections.abc import Mapping
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Dict, List, Union
from datetime import timedelta
import pandas as pd
import pytz
from tenacity import retry, stop_after_attempt, wait_random

from chanlun import fun
from chanlun.persistence.file_lock import (
    InterprocessLockTimeout,
    interprocess_file_lock,
)
from chanlun.exchange.exchange import (
    Exchange,
    SINGLE_SYMBOL_STOCK_INFO,
    Tick,
    convert_stock_kline_frequency,
)
from chanlun.exchange.kline_precision import (
    normalize_kline_precision,
    resolve_structure_price_quantum,
)
from chanlun.exchange.price_basis import (
    QMT_STRUCTURE_DIVIDEND_TYPE,
    attach_price_basis_metadata,
    build_qmt_price_basis_metadata,
)
from chanlun.exchange.qmt_time_contract import qmt_exclusive_download_end
from chanlun.tools.log_util import LogUtil
from xtquant import xtdata


# xtquant 的 native 客户端不是线程安全的：多线程并发调用 download_history_data /
# get_market_data / get_full_tick / get_instrument_detail 等接口时，其内部 BSON
# 序列化层可能触发 `Assertion failed: u < 1000000, file ...\bson\src\bsonobj.cpp`
# 断言，进而以 0xC0000409 (STATUS_STACK_BUFFER_OVERRUN) 强制终止整个 Python 进程，
# Python 层无法 try/except 捕获。这里用进程级全局可重入锁把所有对 xtdata 的调用
# 串行化，作为防御性兜底，避免多线程并发触发崩溃。
_XTDATA_NATIVE_LOCK = threading.RLock()

# Every native worker is a separate Python process, while all of them talk to
# the same MiniQMT service and write the same local history directory.  A
# ``threading.RLock`` therefore cannot protect download_history_data* across
# structure shards.  Production evidence showed concurrent calls returning
# successfully while most target files remained one session behind.  Serialize
# only the mutating download lane across processes; read-only QMT calls retain
# their existing parallelism.
_QMT_DOWNLOAD_INTERPROCESS_LOCK_TIMEOUT_SECONDS = 30.0
_QMT_LIVE_SUBSCRIPTION_INITIAL_CALLBACK_WAIT_SECONDS = 0.5


def _qmt_download_interprocess_lock_path() -> Path:
    configured_data = os.environ.get("CHANLUN_QMT_LOCAL_DATA_DIR", "").strip()
    identity = (
        os.path.normcase(
            os.path.abspath(os.path.expanduser(configured_data))
        )
        if configured_data
        else "default-qmt-endpoint"
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return (
        Path(tempfile.gettempdir())
        / "chanlun-pro-qmt-locks"
        / f"history-download-{digest}.lock"
    )


@contextmanager
def _xtdata_download_interprocess_lock():
    try:
        with interprocess_file_lock(
            _qmt_download_interprocess_lock_path(),
            timeout_seconds=_QMT_DOWNLOAD_INTERPROCESS_LOCK_TIMEOUT_SECONDS,
            poll_seconds=0.01,
        ):
            yield
    except InterprocessLockTimeout as exc:
        raise TimeoutError(
            "timed out waiting for the shared QMT history download lane"
        ) from exc


def _complete_local_history_requirement(
    *, frequency, query_start, end_date, req_counts, observed_at,
):
    """Prove the requested closed grid using the pinned calendar, or decline.

    This is an opt-in download optimization, not a stale-data fallback.  An
    uncovered year, suspension or incomplete local window still downloads.
    """
    from chanlun.exchange.a_share_minute_grid import (
        a_share_completed_one_minute_closes,
    )
    from chanlun.exchange.trading_session import (
        official_trading_session_evidence,
    )

    group_size = {"1m": 1, "5m": 5, "30m": 30}.get(frequency)
    if group_size is None or not end_date:
        return None
    cutoff = pd.Timestamp(end_date)
    cutoff = (
        cutoff.tz_localize(observed_at.tzinfo)
        if cutoff.tzinfo is None else cutoff.tz_convert(observed_at.tzinfo)
    )
    cutoff = min(cutoff, pd.Timestamp(observed_at))
    started = pd.Timestamp(query_start)
    started = (
        started.tz_localize(observed_at.tzinfo)
        if started.tzinfo is None else started.tz_convert(observed_at.tzinfo)
    )
    if started > cutoff:
        return None
    evidence = official_trading_session_evidence(
        session=cutoff.date(), observed_at=observed_at,
    )
    if evidence is None:
        return None
    calendar = evidence["calendar_document"]
    coverage_start = datetime.date.fromisoformat(calendar["coverage_start"])
    if req_counts is None and started.date() < coverage_start:
        return None
    expected = tuple(
        close
        for day in calendar["trading_days"]
        if max(started.date(), coverage_start) <= datetime.date.fromisoformat(day)
        <= cutoff.date()
        for close in a_share_completed_one_minute_closes(
            datetime.date.fromisoformat(day),
        )[group_size - 1::group_size]
        if started <= close <= cutoff
    )
    if req_counts is not None:
        if len(expected) < req_counts:
            return None
        expected = expected[-req_counts:]
    if not expected:
        return None
    return expected, calendar["calendar_fingerprint"], started, cutoff


def _local_history_covers_requirement(frame, *, code, frequency, requirement):
    """Keep real rows/price metadata; never fill gaps or move their clocks."""
    if frame is None or frame.empty:
        return False
    expected, _calendar_revision, started, cutoff = requirement
    columns = {"code", "date", "open", "high", "low", "close", "volume"}
    if not columns.issubset(frame.columns) or not frame["code"].eq(code).all():
        return False
    times = tuple(pd.Timestamp(value) for value in frame["date"])
    if (
        any(pd.isna(value) or value.tzinfo is None for value in times)
        or any(left >= right for left, right in zip(times, times[1:]))
        or times[0] < started or times[-1] > cutoff
    ):
        return False
    expected_set = frozenset(expected)
    observed_grid = tuple(value for value in times if value in expected_set)
    if observed_grid != expected:
        return False
    # QMT can include opening-auction events in a 1m response.  Preserve these
    # actual source rows, but do not count them as completed minute bars.
    session_dates = {value.date() for value in expected}
    if any(
        value not in expected_set
        and not (
            frequency == "1m"
            and value.date() in session_dates
            and value.time() in {datetime.time(9, 25), datetime.time(9, 30)}
        )
        for value in times
    ):
        return False
    for row in frame.itertuples(index=False):
        prices = (row.open, row.high, row.low, row.close)
        if (
            not all(math.isfinite(value) and value > 0 for value in prices)
            or not math.isfinite(row.volume) or row.volume < 0
            or row.low > min(row.open, row.close)
            or row.high < max(row.open, row.close)
        ):
            return False
    return True


class ExchangeQMT(Exchange):
    """QMT（xtquant）沪深 A 股行情适配器。"""

    kline_time_label = "end"
    stock_info_query_scope = SINGLE_SYMBOL_STOCK_INFO
    all_stocks_requires_explicit_authorization = True
    # 实时严格结构可以用显式 start_date 固定一代运行状态的左边界。调用方在
    # 这种模式下不再传 req_counts，避免“最新 N 根”每分钟左移并击穿增量前缀。
    supports_stable_incremental_window = True

    def __init__(self):
        xtdata.enable_hello = False

        self.tz = pytz.timezone("Asia/Shanghai")

        # g_all_stocks 必须为实例属性，并发构建期间用 Lock 保护，
        # 避免多线程同时进入 all_stocks() 各自跑全量扫描，以及类属性多实例共享穿透。
        self.g_all_stocks: list = []
        self._all_stocks_lock = threading.Lock()
        # A subscription is process-local and keeps MiniQMT's in-memory K-line
        # cache continuous without mutating the shared on-disk history store.
        # Native screening shards are long lived and own stable symbol
        # affinity, so each (symbol, period, adjustment) is subscribed once.
        self._live_kline_subscription_lock = threading.Lock()
        self._live_kline_subscriptions: dict[
            tuple[str, str, str], tuple[int, threading.Event, object]
        ] = {}

        # get_market_data 周期映射；"y" 已移除，xtquant 不支持年线 period，传入会触发 BSON 断言崩溃
        self.frequency_map = {
            "1m": "1m",
            "5m": "5m",
            "15m": "15m",
            "30m": "30m",
            "60m": "1h",
            "d": "1d",
            "w": "1w",
            "m": "1mon",
        }

        # download_history_data 周期映射：QMT 下载接口仅支持 1m/5m/1d 基础周期；
        # 15m/30m/60m 用 5m 下载后由 get_market_data 合成，比直接下载高阶周期更灵活
        self.download_frequency_map = {
            "1m": "1m",
            "5m": "5m",
            "15m": "5m",
            "30m": "5m",
            "60m": "5m",
            # 二、十、一百二十分钟周期由一分钟线转换合成，因此下载基础必须是一分钟线；
            # 若错误回退到日线，冷标的会返回空数据。
            "2m": "1m",
            "10m": "1m",
            "120m": "1m",
            "d": "1d",
            "w": "1d",
            "m": "1d",
        }

        # 默认回看窗口来自 _lookback 统一来源，修改请改 _lookback.py。
        # 保留 self.DEFAULT_LOOKBACK 字段供外部访问，类内部通过 get_start_date_by_frequency 使用。
        from chanlun.exchange._lookback import DEFAULT_LOOKBACK_DAYS

        self.DEFAULT_LOOKBACK = {
            freq: timedelta(days=days) for freq, days in DEFAULT_LOOKBACK_DAYS.items()
        }

        # QMT 的历史覆盖单独配置，其他数据源继续使用共享回看设置。
        # 5m 回看 365 天，为本周期笔、线段与中枢提供足够的历史。
        # 1m 覆盖到 60 天(2026-06-25 用户反馈"1m 周期还是太短"):QMT 本地源切标的快、可承受更长
        # 1m;只覆盖 A股,不动共享 _lookback.py → 美股(长桥)1m 仍 30 天(拉 60 天会切标的卡 20-31s)。
        # 历史越长，计算量和图表传输量也越大，需要兼顾首次加载耗时；
        #       且 QMT 实际能回看多少由数据源返回为准(指数/主板通常比个股长,部分个股可能不足 60 天)。
        QMT_LOOKBACK_OVERRIDE_DAYS = {
            "1m": 60,  # A股 1m 拉长(本地源快;US 不动以保切标的速度)
            "5m": 365,  # 本周期结构的历史覆盖
        }
        for _freq, _days in QMT_LOOKBACK_OVERRIDE_DAYS.items():
            self.DEFAULT_LOOKBACK[_freq] = timedelta(days=_days)

    def _ensure_live_kline_subscription(
        self,
        *,
        qmt_code: str,
        period: str,
        dividend_type: str,
        force_refresh: bool = False,
    ) -> bool:
        """Keep one process-local intraday stream current without downloading.

        MiniQMT's documented subscription path places the requested historical
        prefix in the client cache and then keeps pushing new bars.  Waiting for
        the first callback closes the startup race where an immediate local
        read could otherwise still end at the last on-disk download.  Failure
        remains soft: the caller reads the local store and the strict gateway
        will use its exact-symbol download fallback if freshness is not proven.
        """

        if period not in {"1m", "5m"}:
            return False
        key = (qmt_code, period, dividend_type)
        with self._live_kline_subscription_lock:
            existing = self._live_kline_subscriptions.pop(key, None) if force_refresh else (
                self._live_kline_subscriptions.get(key)
            )
            if force_refresh and existing is not None:
                unsubscribe = getattr(xtdata, "unsubscribe_quote", None)
                if callable(unsubscribe):
                    try:
                        with _XTDATA_NATIVE_LOCK:
                            unsubscribe(existing[0])
                    except Exception as exc:
                        LogUtil.warning(
                            "[ExchangeQMT.live_subscription] unsubscribe failed "
                            f"code={qmt_code} period={period} err={exc}"
                        )
                existing = None
            if existing is None:
                subscribe = getattr(xtdata, "subscribe_quote2", None)
                if not callable(subscribe):
                    return False
                ready = threading.Event()

                def on_quote(_payload: object) -> None:
                    ready.set()

                try:
                    with _XTDATA_NATIVE_LOCK:
                        sequence = subscribe(
                            qmt_code,
                            period,
                            start_time=datetime.datetime.now(self.tz).strftime(
                                "%Y%m%d"
                            ),
                            end_time="",
                            count=-1,
                            dividend_type=dividend_type,
                            callback=on_quote,
                        )
                except Exception as exc:
                    LogUtil.warning(
                        "[ExchangeQMT.live_subscription] subscribe failed "
                        f"code={qmt_code} period={period} err={exc}"
                    )
                    return False
                if type(sequence) is not int or sequence <= 0:
                    LogUtil.warning(
                        "[ExchangeQMT.live_subscription] subscribe rejected "
                        f"code={qmt_code} period={period} sequence={sequence!r}"
                    )
                    return False
                existing = (sequence, ready, on_quote)
                self._live_kline_subscriptions[key] = existing
        return existing[1].wait(
            timeout=_QMT_LIVE_SUBSCRIPTION_INITIAL_CALLBACK_WAIT_SECONDS
        )

    def refresh_live_kline_subscription(
        self,
        code: str,
        frequency: str,
        *,
        dividend_type: str = QMT_STRUCTURE_DIVIDEND_TYPE,
    ) -> bool:
        """Force one non-downloading refresh after a strict stale-bar check."""

        if frequency not in {"1m", "5m"}:
            return False
        return self._ensure_live_kline_subscription(
            qmt_code=self.code_to_qmt(code),
            period=self.frequency_map[frequency],
            dividend_type=dividend_type,
            force_refresh=True,
        )

    def code_to_tdx(self, code: str):
        _c = code.split(".")
        if len(_c[0]) == 6:
            return _c[1] + "." + _c[0]
        else:
            return _c[0] + "." + _c[1]

    def code_to_qmt(self, code: str):
        _c = code.split(".")
        if len(_c[0]) == 6:
            return _c[0] + "." + _c[1]
        else:
            return _c[1] + "." + _c[0]

    def default_code(self):
        return "SH.000001"

    def support_frequencys(self):
        return self.frequency_map

    def all_stocks(self, *, full_market_authorized: bool = False):
        if full_market_authorized is not True:
            raise PermissionError(
                "QMT full-market catalog enumeration requires explicit authorization"
            )

        # 双检锁防止并发线程同时进入构建临界区，已就绪后无锁直接返回。
        if len(self.g_all_stocks) > 0:
            return self.g_all_stocks

        with self._all_stocks_lock:
            # 拿到锁后再查一次：可能已经有别的线程构建完了。
            if len(self.g_all_stocks) > 0:
                return self.g_all_stocks

            # 黑名单用 set 避免 5500+ 次 list 线性查找
            black_codes = {
                "SZ.399290",
                "SZ.399289",
                "SZ.399302",
                "SZ.399298",
                "SZ.399481",
                "SZ.399299",
                "SZ.399301",
                "SH.000013",
                "SH.000022",
                "SH.000116",
                "SH.000061",
                "SH.000101",
                "SH.000012",
                "SZ.988201",
                "SZ.980068",
                "SZ.980001",
                "SZ.980023",
            }

            # 全市场扫描放在一把大锁内：避免多线程穿插调用触发 BSON 断言，
            # 同时 inline 拿 instrument_detail 减少 native 调用次数。
            with _XTDATA_NATIVE_LOCK:
                ticks = xtdata.get_full_tick(["SH", "SZ", "BJ"])
                # Quotes are not a security directory: BJ can have usable
                # history while the provider returns no current market ticks.
                # Retain quoted ETF/index entries and include every listed
                # A-share instrument, then classify the actual security type.
                listed_codes = xtdata.get_stock_list_in_sector("沪深京A股")
                tick_codes = list(dict.fromkeys([*ticks, *listed_codes]))

                all_stocks = []
                for _c in tick_codes:
                    _stock_type: dict = xtdata.get_instrument_type(_c)
                    if not (
                        _stock_type.get("stock")
                        or _stock_type.get("etf")
                        or _stock_type.get("index")
                    ):
                        continue

                    tdx_code = self.code_to_tdx(_c)
                    if tdx_code in black_codes:
                        continue

                    try:
                        stock_detail = xtdata.get_instrument_detail(_c, False)
                    except Exception as _e:
                        LogUtil.warning(
                            f"[ExchangeQMT.all_stocks] get_instrument_detail failed code={_c} err={_e}"
                        )
                        continue
                    if not stock_detail:
                        continue

                    if _stock_type.get("stock"):
                        sym_type = "stock_cn"
                    elif _stock_type.get("etf"):
                        sym_type = "etf_cn"
                    else:
                        sym_type = "index_cn"
                    all_stocks.append(
                        {
                            "code": tdx_code,
                            "name": stock_detail["InstrumentName"],
                            "type": sym_type,
                            "precision": fun.reverse_decimal_to_power_of_ten(
                                stock_detail["PriceTick"]
                            ),
                        }
                    )

            self.g_all_stocks = all_stocks
            return self.g_all_stocks

    def get_start_date_by_frequency(
        self, frequency: str, req_counts: int = None
    ) -> str:
        """
        根据周期获取默认起始日期。
        如果上层指定了 req_counts（用户实际只要这么多根 K 线），会按周期估算
        一个紧凑的回看窗口，避免无谓地拉超长历史导致 BSON payload 过大。
        """
        if frequency not in self.download_frequency_map:
            raise ValueError(f"unsupported QMT frequency: {frequency}")
        now = datetime.datetime.now()
        delta = self.DEFAULT_LOOKBACK[frequency]

        # 当调用方指定了请求 K 线数量时，按周期估算一个紧凑的窗口（带 3 倍冗余）。
        # 这样典型场景（300 根 1m）只需要 ~3 天历史，而不是 30 天。
        if req_counts is not None and req_counts > 0:
            intraday_minutes_per_bar = {
                "1m": 1,
                "2m": 2,
                "5m": 5,
                "10m": 10,
                "15m": 15,
                "30m": 30,
                "60m": 60,
                "120m": 120,
            }
            if frequency in intraday_minutes_per_bar:
                # A 股每天仅有约 240 个交易分钟。按 24 小时连续市场折算会把
                # 1,200 根 1m 的窗口压缩成约 3 天，周末后实际不足 600 根。
                required_minutes = intraday_minutes_per_bar[frequency] * req_counts * 3
                trading_days = (required_minutes + 239) // 240
                calendar_days = (trading_days * 7 + 4) // 5
                est_delta = timedelta(days=calendar_days)
            else:
                minutes_per_bar = {
                    "d": 60 * 24,
                    "w": 60 * 24 * 7,
                    "m": 60 * 24 * 30,
                }
                est_minutes = minutes_per_bar[frequency] * req_counts * 3
                est_delta = timedelta(minutes=est_minutes)
            # 取估算窗口和默认窗口中较小的
            if est_delta < delta:
                delta = est_delta

        start_date = now - delta
        return start_date.strftime("%Y%m%d")

    def prewarm_batch_download(
        self,
        codes,
        frequencies,
        cancel_check=None,
        progress_callback=None,
        chunk_size: int = 100,
        req_counts_by_frequency=None,
    ):
        """批量预下载多只标的的基础周期数据到本地 QMT 库,供预热逐只计算时跳过 download。

        加速原理:把"逐只 N 周期"的成千上万次 download_history_data 往返,合并成
        每个基础周期(1m/5m/1d)分块的少量 download_history_data2 批量调用;之后逐只
        klines 传 skip_download=True 只读本地库,不再各自往返。

        ``req_counts_by_frequency`` 可把批量下载限制到调用方实际需要的K线根数，
        避免30m预热默认拉取整年1m基础数据。

        安全:
        - xtquant 线程不安全 → 全程持 _XTDATA_NATIVE_LOCK 串行(与 klines 同锁)。
        - 分块 chunk_size + incrementally=True 控制单次 payload,降低 BSON 0xC0000409 风险。
        - 所有工作进程共享下载写锁；返回值只代表调用完成，后续读取仍须校验本地事实。
        - 单块异常吞掉(warning):该块标的逐只 download 仍能兜底(不传 skip_download 时)。
        - cancel_check() 返回 True 尽快中止。
        """
        frequencies = tuple(frequencies)
        if any(type(freq) is not str for freq in frequencies):
            raise TypeError("QMT frequencies must be exact strings")
        unsupported = set(frequencies) - set(self.download_frequency_map)
        if unsupported:
            raise ValueError(f"unsupported QMT frequencies: {sorted(unsupported)}")
        if req_counts_by_frequency is None:
            req_counts_by_frequency = {}
        if not isinstance(req_counts_by_frequency, Mapping):
            raise TypeError("req_counts_by_frequency must be a mapping")
        unknown_frequencies = set(req_counts_by_frequency) - set(frequencies)
        if unknown_frequencies:
            raise ValueError(
                "req_counts_by_frequency contains an unrequested frequency"
            )
        for frequency, req_counts in req_counts_by_frequency.items():
            if type(frequency) is not str or not frequency:
                raise ValueError("request-count frequency must be a non-empty string")
            if type(req_counts) is not int or req_counts <= 0:
                raise ValueError("request counts must be positive ints")

        # 基础下载周期 → 该周期需覆盖的最早 start(用到它的各 freq 取最长回看)
        base_starts: dict = {}
        for freq in frequencies:
            base = self.download_frequency_map[freq]
            start = self.get_start_date_by_frequency(
                freq,
                req_counts=req_counts_by_frequency.get(freq),
            )
            if base not in base_starts or start < base_starts[base]:
                base_starts[base] = start
        if not base_starts:
            return {
                "schema": "chanlun-qmt-batch-download-result",
                "cancelled": False,
                "successful_by_base": {},
                "failed_by_base": {},
            }
        if type(chunk_size) is not int or chunk_size <= 0:
            raise ValueError("QMT batch download chunk_size must be a positive int")
        raw_codes = tuple(codes)
        if any(type(code) is not str or not code for code in raw_codes):
            raise ValueError("QMT batch download codes must be non-empty strings")
        code_pairs = [(code, self.code_to_qmt(code)) for code in raw_codes]
        if len({code for code, _qmt_code in code_pairs}) != len(code_pairs):
            raise ValueError("QMT batch download codes must be unique")
        total = len(code_pairs)
        successful_by_base: dict[str, set[str]] = {
            base: set() for base in base_starts
        }
        failed_by_base: dict[str, set[str]] = {
            base: set() for base in base_starts
        }
        for base, start in base_starts.items():
            done = 0
            for i in range(0, total, chunk_size):
                if cancel_check and cancel_check():
                    LogUtil.info(
                        f"[ExchangeQMT.batch_download] 取消 base={base} {done}/{total}"
                    )
                    return {
                        "schema": "chanlun-qmt-batch-download-result",
                        "cancelled": True,
                        "successful_by_base": {
                            key: tuple(sorted(values))
                            for key, values in sorted(successful_by_base.items())
                        },
                        "failed_by_base": {
                            key: tuple(sorted(values))
                            for key, values in sorted(failed_by_base.items())
                        },
                    }
                chunk_pairs = code_pairs[i : i + chunk_size]
                chunk = [qmt_code for _code, qmt_code in chunk_pairs]
                with _xtdata_download_interprocess_lock(), _XTDATA_NATIVE_LOCK:
                    try:
                        xtdata.download_history_data2(
                            chunk,
                            base,
                            start_time=start,
                            end_time="",
                            incrementally=True,
                        )
                        successful_by_base[base].update(
                            code for code, _qmt_code in chunk_pairs
                        )
                    except Exception as e:
                        failed_by_base[base].update(
                            code for code, _qmt_code in chunk_pairs
                        )
                        LogUtil.warning(
                            f"[ExchangeQMT.batch_download] chunk 失败 base={base} i={i}: {e}"
                        )
                done += len(chunk)
                if progress_callback:
                    try:
                        progress_callback(base, done, total)
                    except Exception:
                        pass
            LogUtil.info(
                f"[ExchangeQMT.batch_download] base={base} 完成 {done}/{total} start={start}"
            )
        return {
            "schema": "chanlun-qmt-batch-download-result",
            "cancelled": False,
            "successful_by_base": {
                key: tuple(sorted(values))
                for key, values in sorted(successful_by_base.items())
            },
            "failed_by_base": {
                key: tuple(sorted(values))
                for key, values in sorted(failed_by_base.items())
            },
        }

    @retry(stop=stop_after_attempt(3), wait=wait_random(min=0.1, max=1))
    def klines(
        self,
        code: str,
        frequency: str,
        start_date: str = None,
        end_date: str = None,
        args=None,
    ) -> pd.DataFrame:
        return self._klines_once(code, frequency, start_date, end_date, args)

    def _klines_once(self, code, frequency, start_date=None, end_date=None, args=None):
        empty_df = pd.DataFrame(
            columns=["code", "date", "open", "high", "low", "close", "volume"]
        )

        if args is not None:
            if type(args) is not dict:
                raise TypeError("QMT K-line args must be an exact dict")
            unknown_args = set(args) - {
                "req_counts",
                "exact_end",
                "dividend_type",
                "skip_download",
                "prefer_local",
                "incremental_refresh_days",
                "download_start_date",
            }
            if unknown_args:
                raise ValueError(f"unsupported QMT K-line args: {sorted(unknown_args)}")
            if "req_counts" in args and (
                type(args["req_counts"]) is not int or args["req_counts"] <= 0
            ):
                raise ValueError("req_counts must be a positive exact int")
            if "skip_download" in args and type(args["skip_download"]) is not bool:
                raise ValueError("skip_download must be an exact bool")
            if "prefer_local" in args and type(args["prefer_local"]) is not bool:
                raise ValueError("prefer_local must be an exact bool")
            if "incremental_refresh_days" in args and (
                type(args["incremental_refresh_days"]) is not int
                or not 1 <= args["incremental_refresh_days"] <= 60
            ):
                raise ValueError(
                    "incremental_refresh_days must be an exact int inside [1, 60]"
                )
            if "download_start_date" in args:
                value = args["download_start_date"]
                if not isinstance(value, str) or len(value) != 8 or not value.isdigit():
                    raise ValueError("download_start_date must be YYYYMMDD")
                datetime.datetime.strptime(value, "%Y%m%d")
                if "incremental_refresh_days" in args:
                    raise ValueError("download start options cannot be combined")

        # QMT 可服务周期 = 原生(frequency_map) + convert 合成(2m/10m resample, 120m 分段)。
        # 其余(q/y/3m/6m 等)convert 不支持:历史会 fallback 读 1m 再 convert 抛异常,被外层
        # 若进入重试装饰器会连续吞掉三次异常后转成 RetryError；这里与 cq 一致，
        # 在入口如实拒绝，不降级也不重试。
        QMT_SUPPORTED_FREQS = frozenset(self.download_frequency_map)
        if frequency not in QMT_SUPPORTED_FREQS:
            LogUtil.warning(
                f"[ExchangeQMT.klines] 不支持的周期 {frequency} code={code}, 返回空(不降级)"
            )
            return empty_df

        qmt_read_period = (
            self.frequency_map[frequency] if frequency in self.frequency_map else "1m"
        )
        qmt_code = self.code_to_qmt(code)

        qmt_download_period = self.download_frequency_map[frequency]

        # args["req_counts"] 允许上层声明实际需要的 K 线数量，用于收紧回看窗口
        req_counts = args.get("req_counts") if args else None
        if start_date:
            query_start = start_date.replace("-", "").replace(" ", "").replace(":", "")
        else:
            query_start = self.get_start_date_by_frequency(
                frequency, req_counts=req_counts
            )
        if (
            args is not None
            and "exact_end" in args
            and type(args["exact_end"]) is not bool
        ):
            raise ValueError("exact_end must be an exact bool")
        exact_end = args is not None and args.get("exact_end") is True
        if exact_end and not end_date:
            raise ValueError("exact_end requires end_date")
        query_end = (
            end_date.replace("-", "").replace(" ", "").replace(":", "")
            if exact_end
            else ""
        )
        # QMT 下载接口不包含 ``end_time``，读取接口却包含同一边界。下载边界统一后移
        # 一秒，读取和下方裁剪仍固定在业务时刻，既补齐端点 K 线，也不会暴露未来数据。
        download_query_end = query_end
        if exact_end:
            download_query_end = qmt_exclusive_download_end(end_date)

        dividend_type = (
            args.get("dividend_type", QMT_STRUCTURE_DIVIDEND_TYPE)
            if args
            else QMT_STRUCTURE_DIVIDEND_TYPE
        )
        if dividend_type not in {
            "none",
            "front",
            "back",
            "front_ratio",
            "back_ratio",
        }:
            raise ValueError("unsupported QMT dividend_type")

        # download + get_market_data 一起持锁，防止其他线程在两次调用之间插入导致状态错乱
        # incrementally=True 避免全量下载，减小 BSON payload，降低 `u < 1000000` 断言触发概率
        field_list = ["time", "open", "high", "low", "close", "volume"]
        # 预热批量预下载后, 逐只可跳过 download(数据已在本地库), 只读取——省下逐只 QMT 往返。
        # 仅预热路径经 args 显式传入 skip_download=True; 用户实时请求不传, 行为不变。
        _skip_dl = args.get("skip_download", False) if args is not None else False
        if args and args.get("prefer_local") and not _skip_dl:
            try:
                requirement = _complete_local_history_requirement(
                    frequency=frequency, query_start=query_start,
                    end_date=end_date, req_counts=req_counts,
                    observed_at=datetime.datetime.now(self.tz),
                )
                if requirement is not None:
                    local_args = {**args, "skip_download": True,
                                  "exact_end": True}
                    local_args.pop("prefer_local")
                    # One read under the existing native lock.  A failed probe
                    # does not spend three retry budgets before normal download.
                    local = self._klines_once(
                        code, frequency, start_date=query_start,
                        end_date=requirement[3].strftime("%Y-%m-%d %H:%M:%S"),
                        args=local_args,
                    )
                    if _local_history_covers_requirement(
                        local, code=code, frequency=frequency,
                        requirement=requirement,
                    ):
                        local.attrs["qmt_history_read_mode"] = "local_complete"
                        local.attrs["qmt_local_history_verified_through"] = (
                            requirement[0][-1].isoformat()
                        )
                        local.attrs["qmt_local_history_calendar_revision"] = requirement[1]
                        LogUtil.info(
                            f"[ExchangeQMT.klines] complete local history "
                            f"code={code} frequency={frequency} rows={len(local)} "
                            f"through={requirement[0][-1].isoformat()}"
                        )
                        return local
            except Exception as exc:
                LogUtil.debug(
                    f"[ExchangeQMT.klines] local coverage unproved "
                    f"code={code} frequency={frequency}: {type(exc).__name__}: {exc}"
                )
        incremental_refresh_days = (
            args.get("incremental_refresh_days") if args is not None else None
        )
        download_query_start = query_start
        if args is not None and "download_start_date" in args:
            download_query_start = max(query_start, args["download_start_date"])
            if query_end and download_query_start[:8] > query_end[:8]:
                raise ValueError("download_start_date cannot exceed query end")
        if incremental_refresh_days is not None:
            # 实时结构已有完整本地历史时，只需下载最近窗口以补齐新完成 K 线；读取仍从
            # query_start 开始，因而不会缩短用于一、二、三类点识别的完整结构前缀。
            recent_start = (
                datetime.datetime.now()
                - timedelta(days=incremental_refresh_days)
            ).strftime("%Y%m%d")
            download_query_start = max(query_start, recent_start)
        price_basis_factors = None
        if (
            _skip_dl
            and not exact_end
            and not end_date
            and qmt_read_period in {"1m", "5m"}
        ):
            self._ensure_live_kline_subscription(
                qmt_code=qmt_code,
                period=qmt_read_period,
                dividend_type=dividend_type,
            )
        download_guard = (
            nullcontext()
            if _skip_dl
            else _xtdata_download_interprocess_lock()
        )
        with download_guard, _XTDATA_NATIVE_LOCK:
            try:
                if not _skip_dl:
                    xtdata.download_history_data(
                        stock_code=qmt_code,
                        period=qmt_download_period,
                        start_time=download_query_start,
                        end_time=download_query_end,
                        incrementally=True,
                    )
                raw_data = xtdata.get_market_data(
                    field_list=field_list,
                    stock_list=[qmt_code],
                    period=qmt_read_period,
                    start_time=query_start,
                    end_time=query_end,
                    count=-1,
                    dividend_type=dividend_type,
                    fill_data=False,
                )
                if (
                    dividend_type != "none"
                    and isinstance(raw_data, Mapping)
                    and raw_data
                ):
                    price_basis_factors = xtdata.get_divid_factors(qmt_code)
            except Exception as e:
                # native 层抛出的普通异常仍然走外层 retry；
                # 注意：BSON 断言导致的 0xC0000409 进程崩溃 Python 无法捕获，
                # 那种情况只能靠外部进程守护（如 NSSM）拉起。
                LogUtil.warning(
                    f"[ExchangeQMT.klines] xtdata call failed code={qmt_code} "
                    f"freq={frequency} read={qmt_read_period} dl={qmt_download_period} err={e}"
                )
                raise

        if not isinstance(raw_data, Mapping):
            raise TypeError("QMT K-line response must be a field mapping")
        if not raw_data:
            return empty_df
        if set(raw_data) != set(field_list) or any(
            not isinstance(raw_data[field], pd.DataFrame) for field in field_list
        ):
            raise TypeError("QMT K-line response field contract is invalid")
        time_col = raw_data["time"]
        if time_col.empty:
            return empty_df
        shapes = {raw_data[field].shape for field in field_list}
        if len(shapes) != 1 or next(iter(shapes))[0] != 1:
            raise ValueError("QMT K-line response fields must share one-symbol shape")
        data_dict = {
            "date": raw_data["time"].values[0],
            "open": raw_data["open"].values[0],
            "high": raw_data["high"].values[0],
            "low": raw_data["low"].values[0],
            "close": raw_data["close"].values[0],
            "volume": raw_data["volume"].values[0],
        }

        klines_df = pd.DataFrame(data_dict)

        if klines_df.empty:
            return empty_df

        try:
            klines_df["date"] = pd.to_datetime(klines_df["date"], unit="ms", utc=True)
            klines_df["date"] = klines_df["date"].dt.tz_convert(self.tz)

            if frequency in ["d", "w", "m"]:
                # 年线已经删除：frequency_map 不含 y，入口白名单也拒绝 y，
                # 原 ["d","w","m","y"] 的 y 分支是到不了的死代码(审查 L1)。
                klines_df["date"] = klines_df["date"].dt.normalize() + pd.Timedelta(
                    hours=15
                )
        except Exception as e:
            LogUtil.warning(
                f"[exchange_qmt] tz convert failed code={code} freq={frequency}: {e}"
            )
            return empty_df

        klines_df["code"] = code

        klines_df = klines_df[
            ["code", "date", "open", "high", "low", "close", "volume"]
        ]
        cols_to_float = ["open", "high", "low", "close", "volume"]
        klines_df[cols_to_float] = klines_df[cols_to_float].astype(float)

        # 非原生周期（如 2m/10m）通过 convert_stock_kline_frequency 从 1m 合成
        if frequency not in self.frequency_map:
            klines_df = convert_stock_kline_frequency(klines_df, frequency)

        # end_date 历史区间裁剪:本函数原忽略 end_date(只用 start 算 query_start、拉到最新),
        # 历史回放或区间查询可能返回超出 end_date 的未来数据。仅对过去自然日的历史查询
        # 裁剪;实时(end_date 为今日/未来)跳过——否则 d/w/m 规整到当日 15:00 的在制 bar(date 晚于
        # intraday 的 now)会被裸 `date<=now` 误删。
        if end_date:
            try:
                _end_dt = pd.to_datetime(end_date)
                _end_dt = (
                    _end_dt.tz_localize(self.tz)
                    if _end_dt.tzinfo is None
                    else _end_dt.tz_convert(self.tz)
                )
                if (
                    exact_end
                    or _end_dt.date() < datetime.datetime.now(self.tz).date()
                ):
                    klines_df = klines_df[klines_df["date"] <= _end_dt]
            except Exception as _e:
                LogUtil.warning(
                    f"[exchange_qmt] end_date 裁剪跳过 code={code} end={end_date}: {_e}"
                )

        # 按调用方声明的 req_counts 截断，避免返回超过需要的数据
        if args and "req_counts" in args:
            req_counts = args["req_counts"]
            if len(klines_df) > req_counts:
                klines_df = klines_df.iloc[-req_counts:]

        klines_df = normalize_kline_precision(klines_df, "a", code)
        quantum = resolve_structure_price_quantum("a", code)
        if quantum is None:
            raise ValueError("A-share structure price quantum is unavailable")
        metadata = build_qmt_price_basis_metadata(
            code=code,
            adjustment=dividend_type,
            structure_price_quantum=quantum,
            factors=price_basis_factors,
        )
        attach_price_basis_metadata(klines_df, metadata)
        return klines_df

    def stock_info(self, code: str) -> Union[Dict, None]:
        qmt_code = self.code_to_qmt(code)
        with _XTDATA_NATIVE_LOCK:
            stock_detail = xtdata.get_instrument_detail(qmt_code, False)
        if not stock_detail:
            return None
        return {
            "code": code,
            "name": stock_detail["InstrumentName"],
            "precision": fun.reverse_decimal_to_power_of_ten(stock_detail["PriceTick"]),
        }

    def market_data_readiness_probe(self) -> Dict[str, object]:
        """通过一次最小只读 RPC 调用验证 QMT 行情服务真正可用。

        进程存在或端口监听都不能证明 xtdata 协议可用。固定查询浦发银行的合约信息，
        不下载历史行情、不访问账户，也不受盘中或盘后状态影响。
        """

        probe_code = "SH.600000"
        detail = self.stock_info(probe_code)
        if not isinstance(detail, Mapping) or not str(detail.get("name") or "").strip():
            raise RuntimeError("QMT 行情 RPC 未返回有效的探针标的信息")
        return {
            "schema": "chanlun-qmt-market-data-readiness",
            "ready": True,
            "probe_code": probe_code,
            "provider": "QMT_XTDATA",
            "real_account_access": False,
            "real_order_transport": False,
        }

    def ticks(self, codes: List[str]) -> Dict[str, Tick]:
        """
        获取 tick 信息
        """
        ticks = {}
        if len(codes) == 0:
            return ticks
        with _XTDATA_NATIVE_LOCK:
            qmt_ticks = xtdata.get_full_tick([self.code_to_qmt(_c) for _c in codes])
        for _c, _t in qmt_ticks.items():
            ticks[self.code_to_tdx(_c)] = Tick(
                code=self.code_to_tdx(_c),
                last=_t["lastPrice"],
                buy1=_t["bidPrice"][0],
                sell1=_t["askPrice"][0],
                high=_t["high"],
                low=_t["low"],
                open=_t["open"],
                volume=_t["volume"],
                rate=(
                    (_t["lastPrice"] - _t["lastClose"]) / _t["lastClose"] * 100
                    if _t["lastClose"] != 0
                    else 0
                ),
            )

        return ticks

    def get_divid_factors(self, stock_code: str) -> pd.DataFrame:
        """
        获取股票除权除息信息
        """
        with _XTDATA_NATIVE_LOCK:
            df = xtdata.get_divid_factors(self.code_to_qmt(stock_code))
        if df is None or df.empty:
            return None
        df.loc[:, "stock_code"] = stock_code
        df["divid_date"] = pd.to_datetime(df["time"] / 1000, unit="s")
        return df

    def now_trading(self, market: str):
        """
        返回当前是否是交易时间。

        用 self.tz（Asia/Shanghai）避免服务器时区不在 +8 时判错。
        含集合竞价时段 09:15-09:25（A 股该时段有 tick 数据）。

        交易时段：周一至周五
            09:15-09:25 集合竞价
            09:30-11:30 上午连续竞价
            13:00-15:00 下午连续竞价
        """
        now_dt = datetime.datetime.now(self.tz)
        if now_dt.weekday() in [5, 6]:  # 周六日不交易
            return False
        hour = now_dt.hour
        minute = now_dt.minute

        # 集合竞价 09:15-09:25
        if hour == 9 and 15 <= minute < 25:
            return True
        # 上午连续竞价 09:30-09:59
        if hour == 9 and minute >= 30:
            return True
        # 上午 10:00-10:59
        if hour == 10:
            return True
        # 上午 11:00-11:29
        if hour == 11 and minute < 30:
            return True
        # 下午 13:00-14:59
        if hour in (13, 14):
            return True
        return False

    def stock_owner_plate(self, code: str):
        raise Exception("交易所不支持")

    def plate_stocks(self, code: str):
        raise Exception("交易所不支持")
