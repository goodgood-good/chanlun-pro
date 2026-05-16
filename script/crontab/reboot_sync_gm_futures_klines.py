#:  -*- coding: utf-8 -*-
"""定时同步期货 K 线到本地数据库（通过掘金量化 API 获取连续合约）。"""
import datetime
import time
import traceback

import pandas as pd
from gm.api import ADJUST_NONE, get_symbols, history, set_serv_addr, set_token
from tqdm.auto import tqdm

from chanlun import config, fun
from chanlun.exchange.exchange import convert_futures_kline_frequency
from chanlun.exchange.exchange_db import ExchangeDB

# 远程执行时需指定掘金终端地址，详见 https://www.myquant.cn/docs/gm3_faq/154#b244aeed0032526e
set_serv_addr(config.GM_SERVER_ADDR)
set_token(config.GM_TOKEN)

# 取连续合约（symbol 以 99 结尾），去掉 99 后缀作为存储 key
symbols = get_symbols(sec_type1=1060, sec_type2=106004)
run_codes = list(set([_s["symbol"].replace("99", "") for _s in symbols]))

# 排除流动性不足或已退市的品种
exclude_codes = [
    "CZCE.RI",
    "CZCE.JR",
    "CZCE.ZC",
    "CZCE.LR",
    "CZCE.WH",
    "CZCE.WT",
    "CZCE.PX",
    "CZCE.PR",
    "CZCE.ME",
    "CZCE.GN",
    "CZCE.WS",
    "CZCE.SH",
    "CZCE.ER",
    "CZCE.PM",
    "CZCE.QM",
    "CFFEX.TT",
    "CZCE.RO",
    "GFEX.PS",
]
run_codes = [_c for _c in run_codes if _c not in exclude_codes]

# run_codes = [
#     "SHFE.RB",
#     "SHFE.FU",
#     "SHFE.HC",
#     "CZCE.MA",
#     "DCE.V",
# ]

print(run_codes)
print(len(run_codes))

db_ex = ExchangeDB("futures")

# 各周期首次全量拉取的起始时间，后续增量更新
sync_frequencys = {
    "1m": {
        "start": fun.datetime_to_str(
            datetime.datetime.now() - datetime.timedelta(days=180), "%Y-%m-%d 09:00:00"
        )
    },
}
print(sync_frequencys)
# 本地周期 → 掘金 API 周期参数映射
fre_maps = {"1m": "60s"}


error_codes = []


def sync_code(code):
    """增量同步单个期货品种的 K 线，起始时间回退 1 天确保衔接完整。"""
    for f, dt in sync_frequencys.items():
        try:
            while True:
                last_dt = db_ex.query_last_datetime(code, f)

                if last_dt is None:
                    last_dt = dt["start"]

                last_dt = fun.datetime_to_str(
                    fun.str_to_datetime(last_dt, "%Y-%m-%d %H:%M:%S")
                    - datetime.timedelta(days=1),
                    "%Y-%m-%d",
                )
                now_datetime = datetime.datetime.now()
                klines = history(
                    code,
                    fre_maps[f],
                    start_time=fun.str_to_datetime(last_dt, "%Y-%m-%d"),
                    end_time=now_datetime,
                    fields="symbol,frequency,open,close,low,high,volume,position,eob",
                    adjust=ADJUST_NONE,
                    df=True,
                )
                if len(klines) == 0:
                    break
                klines.loc[:, "code"] = klines["symbol"]
                klines.loc[:, "date"] = pd.to_datetime(klines["eob"])
                klines = klines[
                    [
                        "code",
                        "date",
                        "open",
                        "close",
                        "high",
                        "low",
                        "volume",
                        "position",
                    ]
                ]

                tqdm.write(
                    f'Run code {code} frequency {f} last_dt {last_dt} klines len {len(klines)} {klines.iloc[-1]["date"]}'
                )

                db_ex.insert_klines(code, f, klines)
                if len(klines) < 1000:
                    break
        except Exception:
            tqdm.write("执行 %s - %s 同步K线异常" % (code, f))
            print(traceback.format_exc())
            time.sleep(1)
            error_codes.append(code)

    return True


if __name__ == "__main__":

    for _code in tqdm(run_codes):
        sync_code(_code)

    print(error_codes)

    # 按需将 1m 数据合并为其他周期（需要时将 False 改为 True）
    if False:
        for _code in tqdm(
            [
                "SHFE.RB",
                "SHFE.FU",
                "SHFE.HC",
                "CZCE.MA",
                "DCE.V",
            ]
        ):
            convert_code = _code
            ex = ExchangeDB("futures")

            klines = ex.klines(convert_code, "1m", args={"limit": 99999999})
            for _to_f in ["5m"]:
                tqdm.write(f"code: {convert_code} len: {len(klines)}")
                to_klines = convert_futures_kline_frequency(
                    klines, _to_f, process_exchange_type="gm"
                )
                tqdm.write(f"code: {convert_code} to_f: {_to_f} len: {len(to_klines)}")
                ex.insert_klines(convert_code, _to_f, to_klines)

    # 清理多余周期数据（需要时将 False 改为 True）
    if False:
        for _code in tqdm(run_codes):
            ex = ExchangeDB("futures")
            for _f in ["5m"]:
                ex.del_klines_by_code_freq(_code, _f)

    print("Done")
