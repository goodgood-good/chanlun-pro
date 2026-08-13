import datetime
import threading
from collections.abc import Mapping
from typing import Dict, List, Union
from datetime import timedelta
import pandas as pd
import pytz
from tenacity import retry, stop_after_attempt, wait_random

from chanlun import fun
from chanlun.exchange.exchange import Exchange, Tick, convert_stock_kline_frequency
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


class ExchangeQMT(Exchange):
    """QMT（xtquant）沪深 A 股行情适配器。"""

    kline_time_label = "end"

    def __init__(self):
        xtdata.enable_hello = False

        self.tz = pytz.timezone("Asia/Shanghai")

        # g_all_stocks 必须为实例属性，并发构建期间用 Lock 保护，
        # 避免多线程同时进入 all_stocks() 各自跑全量扫描，以及类属性多实例共享穿透。
        self.g_all_stocks: list = []
        self._all_stocks_lock = threading.Lock()

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

        # ===== QMT 专属回看覆盖（只影响 QMT/A股，不动共享 _lookback.py、不影响美股 alpaca/polygon）=====
        # 5m 拉长到 365 天，让 5min 图能装出 30m 同级别中枢(需更长 5m 历史才凑得出 3 段走势类型重合)。
        # 1m 覆盖到 60 天(2026-06-25 用户反馈"1m 周期还是太短"):QMT 本地源切标的快、可承受更长
        # 1m;只覆盖 A股,不动共享 _lookback.py → 美股(长桥)1m 仍 30 天(拉 60 天会切标的卡 20-31s)。
        # 权衡：天数越大 → 1m/递归层级越多，但 K 线越多→计算/BSON payload 越重、首屏越慢;
        #       且 QMT 实际能回看多少由数据源返回为准(指数/主板通常比个股长,部分个股可能不足 60 天)。
        QMT_LOOKBACK_OVERRIDE_DAYS = {
            "1m": 60,  # A股 1m 拉长(本地源快;US 不动以保切标的速度)
            "5m": 365,  # 5min 图的 30m 同级别需更长 5m 历史
        }
        for _freq, _days in QMT_LOOKBACK_OVERRIDE_DAYS.items():
            self.DEFAULT_LOOKBACK[_freq] = timedelta(days=_days)

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

    def all_stocks(self):
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
                tick_codes = list(ticks.keys())

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
        - download_history_data2 阻塞至下完才返回,故返回后 get_market_data 可直接读到。
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
                with _XTDATA_NATIVE_LOCK:
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
        empty_df = pd.DataFrame(
            columns=["code", "date", "open", "high", "low", "close", "volume"]
        )

        if args is not None:
            if type(args) is not dict:
                raise TypeError("QMT K-line args must be an exact dict")
            unknown_args = set(args) - {
                "req_counts",
                "research_exact_end",
                "dividend_type",
                "skip_download",
            }
            if unknown_args:
                raise ValueError(f"unsupported QMT K-line args: {sorted(unknown_args)}")
            if "req_counts" in args and (
                type(args["req_counts"]) is not int or args["req_counts"] <= 0
            ):
                raise ValueError("req_counts must be a positive exact int")
            if "skip_download" in args and type(args["skip_download"]) is not bool:
                raise ValueError("skip_download must be an exact bool")

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
            and "research_exact_end" in args
            and type(args["research_exact_end"]) is not bool
        ):
            raise ValueError("research_exact_end must be an exact bool")
        research_exact_end = args is not None and args.get("research_exact_end") is True
        if research_exact_end and not end_date:
            raise ValueError("research_exact_end requires end_date")
        query_end = (
            end_date.replace("-", "").replace(" ", "").replace(":", "")
            if research_exact_end
            else ""
        )
        # QMT 下载接口不包含 ``end_time``，读取接口却包含同一边界。下载边界统一后移
        # 一秒，读取和下方裁剪仍固定在业务时刻，既补齐端点 K 线，也不会暴露未来数据。
        download_query_end = query_end
        if research_exact_end:
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
        price_basis_factors = None
        with _XTDATA_NATIVE_LOCK:
            try:
                if not _skip_dl:
                    xtdata.download_history_data(
                        stock_code=qmt_code,
                        period=qmt_download_period,
                        start_time=query_start,
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
                    research_exact_end
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

    def all_ticks(self) -> Dict[str, Tick]:
        ticks = {}
        all_stocks = self.all_stocks()
        all_codes = [_s["code"] for _s in all_stocks]
        with _XTDATA_NATIVE_LOCK:
            qmt_ticks = xtdata.get_full_tick(["SH", "SZ", "BJ"])
        for _c, _t in qmt_ticks.items():
            _tdx_code = self.code_to_tdx(_c)
            if _tdx_code not in all_codes:
                continue
            ticks[_tdx_code] = Tick(
                code=_tdx_code,
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

    def balance(self):
        raise Exception("QMT 交易功能在 trader 目录实现")

    def positions(self, code: str = ""):
        raise Exception("QMT 交易功能在 trader 目录实现")

    def order(self, code: str, o_type: str, amount: float, args=None):
        raise Exception("QMT 交易功能在 trader 目录实现")
