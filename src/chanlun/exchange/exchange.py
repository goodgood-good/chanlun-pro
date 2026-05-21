from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, List, Union

import pandas as pd
import pytz


# 统一时区设置
__tz = pytz.timezone("Asia/Shanghai")


@dataclass
class Tick:
    """实时行情快照，buy1/sell1 为盘口一档价格，rate 为涨跌幅百分比。"""

    code: str
    last: float
    buy1: float
    sell1: float
    high: float
    low: float
    open: float
    volume: float
    rate: float = 0


class Exchange(ABC):
    """各数据源/交易所适配器的抽象基类，定义统一行情与交易接口。"""

    @abstractmethod
    def default_code(self) -> str:
        """返回 WEB 端默认展示的标的代码。"""

    @abstractmethod
    def support_frequencys(self) -> dict:
        """返回该数据源支持的周期映射，格式为 {内部周期key: WEB展示名称}。

        例如 {'d': 'Day', '60m': '60m'}。
        """

    @abstractmethod
    def all_stocks(self):
        """获取该市场全量标的列表，每项包含 code/name/type 等字段。"""

    @abstractmethod
    def now_trading(self):
        """返回当前是否处于可交易时段（bool）。"""

    @abstractmethod
    def klines(
        self,
        code: str,
        frequency: str,
        start_date: str = None,
        end_date: str = None,
        args=None,
    ) -> Union[pd.DataFrame, None]:
        """获取 K 线数据，返回含 date/open/high/low/close/volume 列的 DataFrame。

        :param code: 标的代码
        :param frequency: 周期，如 'd'/'60m'/'5m'
        :param start_date: 起始时间字符串，部分数据源不支持
        :param end_date: 结束时间字符串，部分数据源不支持
        :param args: 额外参数，如 {'pages': 8, 'fq': 'qfq'}
        """

    @abstractmethod
    def ticks(self, codes: List[str]) -> Dict[str, Tick]:
        """批量获取实时 Tick 信息，返回 {code: Tick} 字典。"""

    @abstractmethod
    def stock_info(self, code: str) -> Union[Dict, None]:
        """获取标的基本信息（名称、精度等），不存在时返回 None。"""

    @abstractmethod
    def stock_owner_plate(self, code: str):
        """获取标的所属行业/概念板块。

        返回格式：
        {'HY': [{'code': '行业名', 'name': '行业名'}], 'GN': [...]}
        """

    @abstractmethod
    def plate_stocks(self, code: str):
        """获取板块内的成分股列表。

        返回格式：[{'code': 'SH.000001', 'name': '上证指数'}]
        """

    @abstractmethod
    def balance(self):
        """获取账户资产信息（仅交易型适配器实现，行情型抛异常）。"""

    @abstractmethod
    def positions(self, code: str = ""):
        """获取当前持仓信息（仅交易型适配器实现，行情型抛异常）。"""

    @abstractmethod
    def order(self, code: str, o_type: str, amount: float, args=None):
        """下单接口（仅交易型适配器实现，行情型抛异常）。"""


def convert_stock_kline_frequency(klines: pd.DataFrame, to_f: str) -> pd.DataFrame:
    """将沪深 A 股 K 线合成到指定周期，时间戳向后对齐（bar 结束时刻）。

    :param klines: 原始 K 线 DataFrame，需含 date/code/open/high/low/close/volume 列
    :param to_f: 目标周期，如 '5m'/'30m'/'60m'/'120m'/'d'/'w'/'m'
    """
    period_maps = {
        "1m": "1min",
        "2m": "2min",
        "5m": "5min",
        "10m": "10min",
        "15m": "15min",
        "30m": "30min",
        "d": "D",
        "w": "W",
        "m": "M",
    }
    code = klines["code"].iloc[0]

    if to_f in period_maps.keys():
        klines.insert(0, column="date_index", value=klines["date"])
        klines.set_index("date_index", inplace=True)
        period_type = period_maps[to_f]

        agg_dict = {
            "open": "first",
            "close": "last",
            "high": "max",
            "low": "min",
            "volume": "sum",
        }

        # 通达信周/月线时间戳取首个交易日（前对齐），日线及以下取最后一根（后对齐）
        if to_f in ["w", "m"]:
            agg_dict["date"] = "first"
        else:
            agg_dict["date"] = "last"

        period_klines = klines.resample(period_type, label="left", closed="right").agg(
            agg_dict
        )
        period_klines["code"] = code
        period_klines["frequency"] = to_f

        # d/w/m 时间戳统一规整到当日 15:00（A 股收盘），避免 resample 产生的 00:00 被误判为隔天
        if to_f in ["d", "w", "m"]:
            period_klines["date"] = pd.to_datetime(
                {
                    "year": period_klines["date"].dt.year,
                    "month": period_klines["date"].dt.month,
                    "day": period_klines["date"].dt.day,
                }
            ) + pd.Timedelta(hours=15)
            period_klines["date"] = pd.to_datetime(
                period_klines["date"]
            ).dt.tz_localize(__tz)

        if to_f in ["2m", "5m", "10m", "15m", "30m"]:
            period_klines.loc[:, "date"] = period_klines.index
            period_klines["date"] = period_klines["date"] + pd.to_timedelta(
                period_maps[to_f]
            )

        period_klines.dropna(inplace=True)
        period_klines.reset_index(inplace=True)

        return period_klines[
            ["date", "frequency", "code", "high", "low", "open", "close", "volume"]
        ]

    # 60m/120m 无法用 resample 整除，需按 A 股交易时段手工分段合并
    freq_config_maps = {
        "60m": {
            "10:30:00": ["09:00:00+08:00", "10:30:00+08:00"],
            "11:30:00": ["10:31:00+08:00", "11:30:00+08:00"],
            "14:00:00": ["13:00:00+08:00", "14:00:00+08:00"],
            "15:00:00": ["14:01:00+08:00", "15:00:00+08:00"],
        },
        "120m": {
            "11:30:00": ["09:00:00+08:00", "11:30:00+08:00"],
            "15:00:00": ["13:00:00+08:00", "15:00:00+08:00"],
        },
    }
    if to_f not in freq_config_maps.keys():
        raise Exception(f"不支持的转换周期：{to_f}")

    klines["new_dt"] = pd.NaT
    date_only = klines["date"].dt.date.astype(str)
    for new_time_str, range_time_str in freq_config_maps[to_f].items():
        start_time_str, end_time_str = range_time_str
        range_start_dt = pd.to_datetime(date_only + " " + start_time_str)
        range_end_dt = pd.to_datetime(date_only + " " + end_time_str)
        target_new_dt = pd.to_datetime(date_only + " " + new_time_str)

        mask = (klines["date"] >= range_start_dt) & (klines["date"] <= range_end_dt)

        klines.loc[mask, "new_dt"] = target_new_dt

    if klines["new_dt"].isnull().any():
        failed_dates = klines.loc[klines["new_dt"].isnull(), "date"]
        raise Exception(
            f"{code} {to_f} 周期转换时间范围错误，以下时间未能匹配配置： {failed_dates.tolist()}"
        )

    klines_groups = klines.groupby(by=["new_dt"]).agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }
    )
    klines_groups["code"] = code
    klines_groups["frequency"] = to_f
    klines_groups["date"] = klines_groups.index
    # groupby 索引为 naive datetime，合并后补上时区信息
    klines_groups["date"] = pd.to_datetime(klines_groups["date"]).dt.tz_localize(__tz)

    klines_groups.reset_index(drop=True, inplace=True)

    return klines_groups[
        ["date", "frequency", "code", "high", "low", "open", "close", "volume"]
    ]


def convert_currency_kline_frequency(klines: pd.DataFrame, to_f: str) -> pd.DataFrame:
    """将数字货币 K 线合成到指定周期，时间戳前对齐（bar 开始时刻）。

    日线以 UTC+8 08:00 为自然日分割点，与交易所惯例一致。
    """

    period_maps = {
        "2m": "2min",
        "3m": "3min",
        "8m": "8min",
        "5m": "5min",
        "10m": "10min",
        "15m": "15min",
        "30m": "30min",
        "60m": "1H",
        "120m": "2H",
        "3h": "3H",
        "4h": "4H",
        "6h": "6H",
        "d": "D",
    }
    if len(klines) == 0:
        return klines

    code = klines.iloc[0]["code"]

    if to_f == "d":
        # 数字货币日线以 08:00 为换日点，凌晨 00:00-07:59 归属前一自然日
        mask = (klines["date"].dt.time >= pd.to_datetime("08:00:00").time()) | (
            klines["date"].dt.time < pd.to_datetime("08:00:00").time()
        )
        klines = klines.assign(
            trade_day=lambda x: pd.to_datetime(x["date"].dt.date)
            - pd.to_timedelta((x["date"].dt.hour < 8).astype(int), unit="D")
        )
        grouped = klines[mask].groupby("trade_day")
        period_klines = pd.DataFrame(
            {
                "date": grouped["trade_day"]
                .first()
                .apply(lambda x: x.replace(hour=8, minute=0, second=0, tzinfo=__tz)),
                "frequency": to_f,
                "code": grouped["code"].first(),
                "open": grouped["open"].first(),
                "close": grouped["close"].last(),
                "high": grouped["high"].max(),
                "low": grouped["low"].min(),
                "volume": grouped["volume"].sum(),
            }
        )
        period_klines = period_klines.reset_index(drop=True)
        return period_klines[
            ["date", "frequency", "code", "high", "low", "open", "close", "volume"]
        ]

    # 数字货币行情偶有成交量为 0 的空 bar，合并前过滤避免干扰 OHLC 聚合
    klines = klines[klines["volume"] != 0]

    klines.insert(0, column="date_index", value=klines["date"])
    klines.set_index("date_index", inplace=True)
    period_type = period_maps[to_f]

    agg_dict = {
        "date": "first",
        "open": "first",
        "close": "last",
        "high": "max",
        "low": "min",
        "volume": "sum",
    }

    period_klines = klines.resample(period_type, label="right", closed="left").agg(
        agg_dict
    )

    period_klines.loc[:, "date"] = period_klines.index
    period_klines["date"] = period_klines["date"] - pd.to_timedelta(period_maps[to_f])
    period_klines.loc[:, "code"] = code
    period_klines.loc[:, "frequency"] = to_f

    period_klines.dropna(inplace=True)
    period_klines.reset_index(inplace=True)
    period_klines.drop("date_index", axis=1, inplace=True)

    return period_klines[
        ["date", "frequency", "code", "high", "low", "open", "close", "volume"]
    ]


def convert_futures_kline_frequency(
    klines: pd.DataFrame, to_f: str, process_exchange_type="gm"
) -> pd.DataFrame:
    """将国内期货 K 线合成到指定周期，时间戳前对齐（bar 开始时刻）。

    :param klines: 原始 K 线，需含 date/code/open/high/low/close/volume 列
    :param to_f: 目标周期，如 '5m'/'30m'/'60m'/'d'
    :param process_exchange_type: 'gm' 掘金/天勤口径，其他值按标准 30min 边界处理；
        二者在 10:15-10:30 休市期的合并方式不同，导致 30m/60m 边界有差异。
    """
    period_maps = {
        "1m": "1min",
        "2m": "2min",
        "3m": "3min",
        "5m": "5min",
        "6m": "6min",
        "10m": "10min",
        "15m": "15min",
        "d": "D",
        "w": "W",
        "m": "M",
    }
    code = klines.iloc[0]["code"]

    if to_f in period_maps.keys():
        klines.insert(0, column="date_index", value=klines["date"])
        klines.set_index("date_index", inplace=True)
        period_type = period_maps[to_f]
        agg_dict = {
            "code": "first",
            "date": "first",
            "open": "first",
            "close": "last",
            "high": "max",
            "low": "min",
            "volume": "sum",
        }
        if "position" in klines.columns:
            agg_dict["position"] = "last"
        period_klines = klines.resample(period_type, label="right", closed="left").agg(
            agg_dict
        )

        if to_f in ["1m", "3m", "5m", "6m", "10m", "15m"]:
            period_klines["date"] = period_klines.index
            period_klines["date"] = period_klines["date"] - pd.to_timedelta(
                period_maps[to_f]
            )

        period_klines.dropna(inplace=True)
        period_klines.reset_index(inplace=True)
        period_klines.drop("date_index", axis=1, inplace=True)
        return period_klines[["code", "date", "open", "close", "high", "low", "volume"]]

    # 10:15-10:30 为期货休市，掘金把该段并入前一个 bar 凑足分钟数，天勤则按自然 30min 边界切割
    if process_exchange_type == "gm":
        freq_config_maps = {
            # 掘金口径：凑够符合分钟数的数据（有夜盘的品种仍有差异，暂不处理）
            "30m": {
                "09:00:00": ["09:00:00", "09:29:59"],
                "09:30:00": ["09:30:00", "09:59:59"],
                "10:00:00": ["10:00:00", "10:44:59"],
                "10:45:00": ["10:45:00", "11:14:59"],
                "11:15:00": ["11:15:00", "13:44:59"],
                "13:45:00": ["13:45:00", "14:14:59"],
                "14:15:00": ["14:15:00", "14:44:59"],
                "14:45:00": ["14:45:00", "14:59:59"],
                "21:00:00": ["21:00:00", "21:29:59"],
                "21:30:00": ["21:30:00", "21:59:59"],
                "22:00:00": ["21:00:00", "22:29:59"],
                "22:30:00": ["21:30:00", "22:59:59"],
                "23:00:00": ["23:00:00", "23:29:59"],
                "23:30:00": ["23:30:00", "23:59:59"],
                "00:00:00": ["00:00:00", "00:29:59"],
                "00:30:00": ["00:30:00", "00:59:59"],
                "01:00:00": ["01:00:00", "01:29:59"],
                "01:30:00": ["01:30:00", "01:59:59"],
                "02:00:00": ["02:00:00", "02:29:59"],
                "02:30:00": ["02:30:00", "02:59:59"],
            },
            "60m": {
                "09:00:00": ["09:00:00", "09:59:59"],
                "10:00:00": ["10:00:00", "11:14:59"],
                "11:15:00": ["11:15:00", "14:14:59"],
                "14:15:00": ["14:15:00", "14:59:59"],
                "21:00:00": ["21:00:00", "21:59:59"],
                "22:00:00": ["21:00:00", "22:59:59"],
                "23:00:00": ["23:00:00", "23:59:59"],
                "00:00:00": ["00:00:00", "00:59:59"],
                "01:00:00": ["01:00:00", "01:59:59"],
                "02:00:00": ["02:00:00", "02:59:59"],
            },
        }
    else:
        freq_config_maps = {
            "30m": {
                "09:00:00": ["09:00:00", "09:29:59"],
                "09:30:00": ["09:30:00", "09:59:59"],
                "10:00:00": ["10:00:00", "10:29:59"],
                "10:30:00": ["10:30:00", "10:59:59"],
                "11:00:00": ["11:00:00", "11:29:59"],
                "11:30:00": ["11:30:00", "11:59:59"],
                "13:00:00": ["13:00:00", "13:29:59"],
                "13:30:00": ["13:30:00", "13:59:59"],
                "14:00:00": ["14:00:00", "14:29:59"],
                "14:30:00": ["14:30:00", "14:59:59"],
                "21:00:00": ["21:00:00", "21:29:59"],
                "21:30:00": ["21:30:00", "21:59:59"],
                "22:00:00": ["22:00:00", "22:29:59"],
                "22:30:00": ["22:30:00", "22:59:59"],
                "23:00:00": ["23:00:00", "23:29:59"],
                "23:30:00": ["23:30:00", "23:59:59"],
                "00:00:00": ["00:00:00", "00:29:59"],
                "00:30:00": ["00:30:00", "00:59:59"],
                "01:00:00": ["01:00:00", "01:29:59"],
                "01:30:00": ["01:30:00", "01:59:59"],
                "02:00:00": ["02:00:00", "02:29:59"],
                "02:30:00": ["02:30:00", "02:59:59"],
            },
            "60m": {
                "09:00:00": ["09:00:00", "09:59:59"],
                "10:00:00": ["10:00:00", "10:59:59"],
                "11:00:00": ["11:00:00", "11:59:59"],
                "13:00:00": ["13:00:00", "13:59:59"],
                "14:00:00": ["14:00:00", "15:00:00"],
                "21:00:00": ["21:00:00", "21:59:59"],
                "22:00:00": ["22:00:00", "22:59:59"],
                "23:00:00": ["23:00:00", "23:59:59"],
                "00:00:00": ["00:00:00", "00:59:59"],
                "01:00:00": ["01:00:00", "01:59:59"],
                "02:00:00": ["02:00:00", "02:59:59"],
            },
        }

    if to_f not in freq_config_maps.keys():
        raise Exception(f"不支持的转换周期：{to_f}")

    klines["new_dt"] = pd.Series(dtype="datetime64[ns, Asia/Shanghai]")
    # 夜盘跨零点：00:00 的 bar 实际属于前一日夜盘最后一根，减 1 分钟归入正确分段
    mask = (klines["date"].dt.hour == 0) & (klines["date"].dt.minute == 0)
    klines.loc[mask, "date"] = klines.loc[mask, "date"] - pd.Timedelta(minutes=1)

    date_only = klines["date"].dt.normalize()
    for new_time_str, range_time_str in freq_config_maps[to_f].items():
        start_time_str, end_time_str = range_time_str
        start_time = pd.to_timedelta(start_time_str)
        end_time = pd.to_timedelta(end_time_str)

        range_start_dt = date_only + start_time

        if end_time_str == "00:00:00":
            range_end_dt = date_only + pd.Timedelta(days=1)
        else:
            range_end_dt = date_only + end_time
        if end_time_str == "00:00:00":
            target_new_dt = date_only + pd.Timedelta(days=1)
        else:
            target_new_dt = date_only + pd.to_timedelta(new_time_str)

        mask = (klines["date"] >= range_start_dt) & (klines["date"] <= range_end_dt)
        klines.loc[mask, "new_dt"] = target_new_dt

    if klines["new_dt"].isnull().any():
        failed_dates = klines.loc[klines["new_dt"].isnull(), "date"]
        raise Exception(
            f"期货周期转换时间范围错误，{code} - {to_f} 以下时间未能匹配配置： {failed_dates.tolist()}"
        )
    agg_config = {
        "code": "first",
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }
    if "position" in klines.columns:
        agg_config["position"] = "last"
    klines_groups = klines.groupby(by=["new_dt"]).agg(agg_config)
    klines_groups["date"] = klines_groups.index
    klines_groups.reset_index(drop=True, inplace=True)

    return klines_groups[["code", "date", "open", "close", "high", "low", "volume"]]


def convert_tdx_futures_kline_frequency(
    klines: pd.DataFrame, to_f: str
) -> pd.DataFrame:
    """将通达信期货 K 线合成到指定周期，时间戳后对齐（bar 结束时刻）。

    通达信期货行情时间戳为 bar 结束时刻，与掘金/天勤（前对齐）相反，需用不同的 resample 参数。
    """
    period_maps = {
        "2m": "2min",
        "3m": "3min",
        "5m": "5min",
        "6m": "6min",
        "10m": "10min",
        "15m": "15min",
        "d": "D",
        "w": "W",
        "m": "M",
    }
    code = klines.iloc[0]["code"]

    if to_f in period_maps.keys():
        if to_f in ["d", "w"]:
            # 夜盘（21-02 点）数据属于次一交易日，整体偏移 3h 后 resample 才能归到正确的自然日
            _night_mask = klines["date"].dt.hour.isin([21, 22, 23, 0, 1, 2])
            klines["date"] = klines["date"].mask(
                _night_mask, klines["date"] + pd.Timedelta(hours=3)
            )

        klines.insert(0, column="date_index", value=klines["date"])
        klines.set_index("date_index", inplace=True)
        period_type = period_maps[to_f]
        # 通达信后对齐：label='left'、closed='right' 对应 bar 结束时刻
        agg_dict = {
            "date": "last",
            "open": "first",
            "close": "last",
            "high": "max",
            "low": "min",
            "volume": "sum",
        }
        period_klines = klines.resample(period_type, label="left", closed="right").agg(
            agg_dict
        )
        # d/w 统一设置为 15:00，与 A 股期货收盘时间对齐，方便多市场对比
        if to_f in ["d", "w"]:
            period_klines["date"] = pd.to_datetime(
                {
                    "year": period_klines["date"].dt.year,
                    "month": period_klines["date"].dt.month,
                    "day": period_klines["date"].dt.day,
                }
            ) + pd.Timedelta(hours=15)
            period_klines["date"] = pd.to_datetime(
                period_klines["date"]
            ).dt.tz_localize(__tz)

        if to_f in ["2m", "5m", "6m", "10m", "15m"]:
            period_klines.loc[:, "date"] = period_klines.index
            period_klines["date"] = period_klines["date"] + pd.to_timedelta(
                period_maps[to_f]
            )
        period_klines = period_klines.dropna().reset_index()

        period_klines["code"] = code
        period_klines["frequency"] = to_f

        return period_klines[
            ["date", "frequency", "code", "high", "low", "open", "close", "volume"]
        ]

    freq_config_maps = {
        "30m": {
            "09:30:00": ["09:00:00", "09:30:00"],
            "10:00:00": ["09:31:00", "10:00:00"],
            "10:45:00": ["10:01:00", "10:45:00"],
            "11:15:00": ["10:46:00", "11:15:00"],
            "13:45:00": ["11:16:00", "13:45:00"],
            "14:15:00": ["13:46:00", "14:15:00"],
            "14:45:00": ["14:16:00", "14:45:00"],
            "15:00:00": ["14:46:00", "15:00:00"],
            "21:30:00": ["21:00:00", "21:30:00"],
            "22:00:00": ["21:31:00", "22:00:00"],
            "22:30:00": ["22:01:00", "22:30:00"],
            "23:00:00": ["22:31:00", "23:00:00"],
            "23:30:00": ["23:01:00", "23:30:00"],
            "00:00:00": ["23:31:00", "00:00:00"],
            "00:30:00": ["00:01:00", "00:30:00"],
            "01:00:00": ["00:31:00", "01:00:00"],
            "01:30:00": ["01:01:00", "01:30:00"],
            "02:00:00": ["01:31:00", "02:00:00"],
            "02:30:00": ["02:01:00", "02:30:00"],
        },
        "60m": {
            "10:00:00": ["09:00:00", "10:00:00"],
            "11:15:00": ["10:01:00", "11:15:00"],
            "14:15:00": ["11:16:00", "14:15:00"],
            "15:00:00": ["14:16:00", "15:00:00"],
            "22:00:00": ["21:00:00", "22:00:00"],
            "23:00:00": ["22:01:00", "23:00:00"],
            "00:00:00": ["23:01:00", "00:00:00"],
            "01:00:00": ["00:01:00", "01:00:00"],
            "02:00:00": ["01:01:00", "02:00:00"],
        },
    }
    # 黄金(AU)/白银(AG)/原油(SC)/商品指数(TI.T) 夜盘延伸至 02:30，60m 分段与普通品种不同
    if (
        code.startswith("QS.AU")
        or code.startswith("QS.AG")
        or code.startswith("QS.SC")
        or code.startswith("TI.T")
    ):
        freq_config_maps["60m"] = {
            "09:30:00": ["02:01:00", "09:30:00"],
            "10:45:00": ["09:31:00", "10:45:00"],
            "13:45:00": ["10:46:00", "13:45:00"],
            "14:45:00": ["13:46:00", "14:45:00"],
            "15:00:00": ["14:46:00", "15:00:00"],
            "22:00:00": ["21:00:00", "22:00:00"],
            "23:00:00": ["22:01:00", "23:00:00"],
            "00:00:00": ["23:01:00", "00:00:00"],
            "01:00:00": ["00:01:00", "01:00:00"],
            "02:00:00": ["01:01:00", "02:00:00"],
        }

    # 中金所股指/国债期货（IF/IH/IC/IM/TL/T/TF/TS）无夜盘且收盘 15:15，分段边界与商品期货不同
    if (
        code.startswith("CZ.TL")
        or code.startswith("CZ.T")
        or code.startswith("CZ.TF")
        or code.startswith("CZ.TS")
        or code.startswith("CZ.IC")
        or code.startswith("CZ.IH")
        or code.startswith("CZ.IM")
        or code.startswith("CZ.IF")
    ):
        freq_config_maps["30m"] = {
            "10:00:00": ["09:30:00", "10:00:00"],
            "10:30:00": ["10:01:00", "10:30:00"],
            "11:00:00": ["10:31:00", "11:00:00"],
            "11:30:00": ["11:01:00", "11:30:00"],
            "13:30:00": ["13:01:00", "13:30:00"],
            "14:00:00": ["13:31:00", "14:00:00"],
            "14:30:00": ["14:01:00", "14:30:00"],
            "15:00:00": ["14:31:00", "15:00:00"],
            "15:15:00": ["15:01:00", "15:15:00"],
        }
        freq_config_maps["60m"] = {
            "10:30:00": ["09:30:00", "10:30:00"],
            "11:30:00": ["10:31:00", "11:30:00"],
            "14:00:00": ["13:00:00", "14:00:00"],
            "15:00:00": ["14:01:00", "15:00:00"],
            "15:15:00": ["15:01:00", "15:15:00"],
        }

    if to_f not in freq_config_maps.keys():
        raise Exception(f"不支持的转换周期：{to_f}")

    klines["new_dt"] = pd.Series(dtype="datetime64[ns, Asia/Shanghai]")
    # 夜盘跨零点：00:00 的 bar 实际属于前一日夜盘最后一根，减 1 分钟归入正确分段
    mask = (klines["date"].dt.hour == 0) & (klines["date"].dt.minute == 0)
    klines.loc[mask, "date"] = klines.loc[mask, "date"] - pd.Timedelta(minutes=1)

    date_only = klines["date"].dt.normalize()
    for new_time_str, range_time_str in freq_config_maps[to_f].items():
        start_time_str, end_time_str = range_time_str
        start_time = pd.to_timedelta(start_time_str)
        end_time = pd.to_timedelta(end_time_str)

        range_start_dt = date_only + start_time

        if end_time_str == "00:00:00":
            range_end_dt = date_only + pd.Timedelta(days=1)
        else:
            range_end_dt = date_only + end_time
        if end_time_str == "00:00:00":
            target_new_dt = date_only + pd.Timedelta(days=1)
        else:
            target_new_dt = date_only + pd.to_timedelta(new_time_str)

        mask = (klines["date"] >= range_start_dt) & (klines["date"] <= range_end_dt)
        klines.loc[mask, "new_dt"] = target_new_dt

    if klines["new_dt"].isnull().any():
        failed_dates = klines.loc[klines["new_dt"].isnull(), "date"]
        raise Exception(
            f"期货周期转换时间范围错误，{code} - {to_f} 以下时间未能匹配配置： {failed_dates.tolist()}"
        )

    agg_config = {
        "code": "first",
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }

    klines_groups = klines.groupby(by=["new_dt"]).agg(agg_config)
    klines_groups["date"] = klines_groups.index
    klines_groups["frequency"] = to_f
    klines_groups.reset_index(drop=True, inplace=True)

    return klines_groups[
        ["date", "frequency", "code", "high", "low", "open", "close", "volume"]
    ]


def convert_us_kline_frequency(klines: pd.DataFrame, to_f: str) -> pd.DataFrame:
    """将美股 K 线（IB/盈透证券口径）合成到指定周期，时间戳前对齐（bar 开始时刻）。"""
    period_maps = {
        "2m": "2min",
        "5m": "5min",
        "10m": "10min",
        "15m": "15min",
        "30m": "30min",
        "60m": "1H",
        "120m": "2H",
        "d": "D",
        "w": "W",
        "m": "M",
    }
    if len(klines) == 0:
        return None
    klines.insert(0, column="date_index", value=klines["date"])
    klines.set_index("date_index", inplace=True)
    period_type = period_maps[to_f]
    agg_dict = {
        "code": "first",
        "date": "first",
        "open": "first",
        "close": "last",
        "high": "max",
        "low": "min",
        "volume": "sum",
    }

    if to_f in ["w"]:  # IB 周线取周末最后一根的时间戳
        agg_dict["date"] = "last"

    period_klines = klines.resample(period_type, label="right", closed="left").agg(
        agg_dict
    )

    period_klines.dropna(inplace=True)
    period_klines.reset_index(inplace=True)
    period_klines.drop("date_index", axis=1, inplace=True)

    return period_klines[["code", "date", "open", "close", "high", "low", "volume"]]


def convert_us_tdx_kline_frequency(klines: pd.DataFrame, to_f: str) -> pd.DataFrame:
    """将美股 K 线（通达信口径）合成到指定周期，时间戳后对齐（bar 结束时刻）。

    通达信美股行情时间为 UTC+8 存储，resample 前需先转 UTC 才能按自然小时对齐。
    """
    period_maps = {
        "2m": "2min",
        "5m": "5min",
        "10m": "10min",
        "15m": "15min",
        "30m": "30min",
        "60m": "1H",
        "120m": "2H",
        "d": "D",
        "w": "W",
        "m": "M",
    }
    klines.insert(
        0,
        column="date_index",
        value=klines["date"].dt.tz_convert(pytz.UTC),
    )
    klines.set_index("date_index", inplace=True)
    period_type = period_maps[to_f]
    agg_dict = {
        "code": "first",
        "date": "last",
        "open": "first",
        "close": "last",
        "high": "max",
        "low": "min",
        "volume": "sum",
    }
    period_klines = klines.resample(period_type, label="left", closed="right").agg(
        agg_dict
    )

    period_klines.dropna(inplace=True)
    period_klines.reset_index(inplace=True)
    period_klines.drop("date_index", axis=1, inplace=True)

    return period_klines[["code", "date", "open", "high", "low", "close", "volume"]]


def get_ny_future_trade_day(dt: pd.Timestamp) -> pd.Timestamp:
    """计算纽约期货所属交易日（06:00 前的 bar 归属前一自然日）。"""
    if dt.hour < 6:
        trade_day = (dt - pd.Timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    else:
        trade_day = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return trade_day


def get_ny_future_trade_week(dt: pd.Timestamp) -> pd.Timestamp:
    """计算纽约期货所属交易周的周一日期（周起始对齐）。"""
    trade_day = get_ny_future_trade_day(dt)
    week_start = trade_day - pd.Timedelta(days=trade_day.weekday())
    return week_start


def convert_tdx_ny_f_kline_frequency(klines: pd.DataFrame, to_f: str) -> pd.DataFrame:
    """将通达信纽约期货 K 线合成到指定周期。

    交易时段为周一至周五 06:00 至次日 05:59（北京时间），06:00 前的 bar 归属前一自然日。
    """

    period_maps = {
        "5m": "5min",
        "15m": "15min",
        "30m": "30min",
        "60m": "60min",
        "d": "D",
        "w": "W",
    }

    code = klines["code"].iloc[0]

    # 纽约期货成交量为 0 的 bar 无意义，过滤后再合并
    klines = klines[klines["volume"] != 0]

    if to_f == "d":
        klines = klines.copy()
        # 向量化计算交易日：hour < 6 的 bar 归属前一自然日
        _td_norm = klines["date"].dt.normalize()
        _td_mask = klines["date"].dt.hour < 6
        klines["trade_day"] = _td_norm.mask(_td_mask, _td_norm - pd.Timedelta(days=1))
        grouped = klines.groupby("trade_day")
        # trade_day 已 normalize，直接 +15h 作为日线时间戳
        _day_dates = grouped["trade_day"].first()
        period_klines = pd.DataFrame(
            {
                "date": _day_dates + pd.Timedelta(hours=15),
                "frequency": to_f,
                "code": grouped["code"].first(),
                "open": grouped["open"].first(),
                "close": grouped["close"].last(),
                "high": grouped["high"].max(),
                "low": grouped["low"].min(),
                "volume": grouped["volume"].sum(),
            }
        )
        period_klines = period_klines.reset_index(drop=True)
        return period_klines[
            ["date", "frequency", "code", "high", "low", "open", "close", "volume"]
        ]
    elif to_f == "w":
        klines = klines.copy()
        # 先算 trade_day（同日线逻辑），再向后对齐到本周一作为 groupby key
        _td_norm = klines["date"].dt.normalize()
        _td_mask = klines["date"].dt.hour < 6
        _trade_day = _td_norm.mask(_td_mask, _td_norm - pd.Timedelta(days=1))
        klines["trade_week"] = _trade_day - pd.to_timedelta(
            _trade_day.dt.dayofweek, unit="D"
        )
        grouped = klines.groupby("trade_week")
        # 周线时间戳取本周最后一根 bar 的自然日 +15h
        _week_max_dates = grouped["date"].max()
        period_klines = pd.DataFrame(
            {
                "date": _week_max_dates.dt.normalize() + pd.Timedelta(hours=15),
                "frequency": to_f,
                "code": grouped["code"].first(),
                "open": grouped["open"].first(),
                "close": grouped["close"].last(),
                "high": grouped["high"].max(),
                "low": grouped["low"].min(),
                "volume": grouped["volume"].sum(),
            }
        )
        period_klines = period_klines.reset_index(drop=True)
        return period_klines[
            ["date", "frequency", "code", "high", "low", "open", "close", "volume"]
        ]
    else:
        klines = klines.copy()
        klines.insert(0, column="date_index", value=klines["date"])
        klines.set_index("date_index", inplace=True)
        period_type = period_maps[to_f]

        agg_dict = {
            "date": "last",
            "open": "first",
            "close": "last",
            "high": "max",
            "low": "min",
            "volume": "sum",
        }
        period_klines = klines.resample(period_type, label="left", closed="right").agg(
            agg_dict
        )
        period_klines["code"] = code
        period_klines["frequency"] = to_f

        period_klines.dropna(inplace=True)
        if to_f in ["5m", "15m", "30m", "60m"]:
            period_klines.loc[:, "date"] = period_klines.index
            period_klines["date"] = period_klines["date"] + pd.to_timedelta(
                period_maps[to_f]
            )

        period_klines.reset_index(inplace=True)
        return period_klines[
            ["date", "frequency", "code", "high", "low", "open", "close", "volume"]
        ]


def convert_kline_frequency(
    klines: pd.DataFrame, to_f: str, dt_align_type: str = "eob"
) -> pd.DataFrame:
    """通用 K 线周期合成，支持 eob（后对齐）和 bob（前对齐）两种时间戳模式。

    :param dt_align_type: 'eob'（end-of-bar，bar 结束时刻，默认）或 'bob'（begin-of-bar，bar 开始时刻）。
        应与原始 K 线的对齐方式一致，错误匹配会导致 OHLC 取错方向。
    """
    period_maps = {
        "2m": "2min",
        "3m": "3min",
        "5m": "5min",
        "10m": "10min",
        "15m": "15min",
        "30m": "30min",
        "60m": "1H",
        "120m": "2H",
        "d": "D",
        "w": "W",
    }
    if len(klines) == 0:
        return None
    klines.insert(0, column="date_index", value=klines["date"])
    klines.set_index("date_index", inplace=True)
    period_type = period_maps[to_f]

    agg_dict = {
        "code": "first",
        "open": "first",
        "close": "last",
        "high": "max",
        "low": "min",
        "volume": "sum",
    }
    if "position" in klines.columns:
        agg_dict["position"] = "last"

    if dt_align_type == "bob":
        agg_dict["date"] = "first"
        period_klines = klines.resample(period_type, label="right", closed="left").agg(
            agg_dict
        )
    else:
        agg_dict["date"] = "last"
        period_klines = klines.resample(period_type, label="left", closed="right").agg(
            agg_dict
        )

    period_klines.dropna(inplace=True)
    period_klines.reset_index(inplace=True)
    period_klines.drop("date_index", axis=1, inplace=True)

    return period_klines


if __name__ == "__main__":
    import pandas as pd

    from chanlun.exchange.exchange_tq import ExchangeTq

    ex = ExchangeTq()
    code = ex.default_code()
    to_f = "15m"

    klines_1m = ex.klines(code, "1m")

    print(f"1分钟k线数据：{len(klines_1m)}")
    print(klines_1m.tail(10))

    src_klines_to_f = ex.klines(code, to_f)
    print(f"原始 {to_f} 数据")
    print(src_klines_to_f.tail(10))

    convert_ch = convert_futures_kline_frequency(
        klines_1m, to_f, process_exchange_type="tq"
    )
    print(f"转换后的 {to_f} 数据")
    print(convert_ch.tail(10))
    print("Done")
