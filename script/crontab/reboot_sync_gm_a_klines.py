#:  -*- coding: utf-8 -*-
"""
定时同步 A 股行情到本地数据库（通过掘金量化 API）。

同步掘金不复权数据（代码前缀 SHSE/SZSE）进行增量更新；
按需将不复权数据复权并合并为其他周期（代码前缀 SH/SZ），供历史选股与策略回测使用。
"""
import datetime
import time
import traceback
import pandas as pd
from gm.api import ADJUST_NONE, get_symbols, history, set_serv_addr, set_token
from tqdm.auto import tqdm

from chanlun import config, fun
from chanlun.exchange.exchange import convert_stock_kline_frequency
from chanlun.exchange.exchange_db import ExchangeDB
from chanlun.exchange.exchange_tdx import ExchangeTDX


def to_tdx_codes(_codes):
    return [_code.replace("SHSE.", "SH.").replace("SZSE.", "SZ.") for _code in _codes]


# 远程执行时需指定掘金终端地址，详见 https://www.myquant.cn/docs/gm3_faq/154#b244aeed0032526e
set_serv_addr(config.GM_SERVER_ADDR)
set_token(config.GM_TOKEN)

symbols = get_symbols(sec_type1=1010, sec_type2=101001)
run_codes = [_s["exchange"] + "." + _s["sec_id"] for _s in symbols]
for _c in [
    "SH.000001",  # 上证指数
    "SZ.399001",  # 深圳指数
    "SZ.399006",  # 创业板指
    "SH.000852",  # 中证1000
]:
    run_codes.append(_c.replace("SZ.", "SZSE.").replace("SH.", "SHSE."))

print("Sync Len : ", len(run_codes))


db_ex = ExchangeDB("a")

tdx_ex = ExchangeTDX()

# 各周期首次全量拉取的起始时间，后续增量更新
sync_frequencys = {
    "d": {
        "start": "1992-01-01",
    },
    "5m": {
        "start": fun.datetime_to_str(
            datetime.datetime.now() - datetime.timedelta(days=170), "%Y-%m-%d"
        )
    },
    # "1m": {
    #     "start": fun.datetime_to_str(
    #         datetime.datetime.now() - datetime.timedelta(days=170), "%Y-%m-%d"
    #     )
    # },
}


print(sync_frequencys)
# 本地周期 → 掘金 API 周期参数映射
fre_maps = {"d": "1d", "5m": "300s", "1m": "60s"}


def sync_code(code):
    """增量同步单个掘金代码的 K 线，起始时间回退 1 天确保衔接完整。"""
    for f, dt in sync_frequencys.items():
        try:
            last_dt = db_ex.query_last_datetime(code, f)
            if last_dt is None:
                last_dt = dt["start"]
            last_dt = fun.datetime_to_str(
                fun.str_to_datetime(last_dt, "%Y-%m-%d") - datetime.timedelta(days=1),
                "%Y-%m-%d",
            )

            now_datetime = datetime.datetime.now()
            klines = history(
                code,
                fre_maps[f],
                start_time=last_dt,
                end_time=now_datetime,
                adjust=ADJUST_NONE,
                df=True,
            )
            if len(klines) == 0:
                break
            klines.loc[:, "code"] = klines["symbol"]
            klines.loc[:, "date"] = pd.to_datetime(klines["eob"])
            klines = klines[["code", "date", "open", "close", "high", "low", "volume"]]

            tqdm.write(
                f"Run code {code} frequency {f} last_dt {last_dt} klines len {len(klines)} 【{klines.iloc[0]['date']} - {klines.iloc[-1]['date']}】"
            )
            db_ex.insert_klines(code, f, klines)
        except Exception:
            print("执行 %s - %s 同步K线异常" % (code, f))
            print(traceback.format_exc())
            time.sleep(10)

    return True


def convert_code(code):
    """
    将掘金不复权 K 线复权并合并为目标周期，写入以 SH/SZ 前缀存储的表。

    增量路径：对比末根 K 线收盘价是否一致，一致则追加；不一致（复权因子变化）则全量重算。
    """
    try:
        db_code = to_tdx_codes([code])[0]
        market, tdx_code, _type = tdx_ex.to_tdx_code(db_code)
        xdxr = tdx_ex.xdxr(market, db_code, tdx_code)

        # 根据 5m 和 d 周期合并&复权生成复权数据，按需修改目标周期列表
        for f in ["d"]:
            gm_freq = "5m"
            if f in ["60m", "30m", "15m", "5m"]:
                gm_freq = "5m"
            elif f in ["d", "w"]:
                gm_freq = "d"
            last_klines = db_ex.klines(db_code, f, args={"limit": 10})
            if len(last_klines) != 0:
                try:
                    last_dt = fun.datetime_to_str(last_klines.iloc[0]["date"])
                    gm_klines = db_ex.klines(
                        code,
                        gm_freq,
                        start_date=last_dt,
                        args={"limit": 9999999},
                    )
                    gm_klines["volume"] = gm_klines["volume"] / 100  # 掘金成交量单位为手，换算为股
                    gm_klines = tdx_ex.klines_fq(gm_klines, xdxr, "qfq")
                    _ks = convert_stock_kline_frequency(gm_klines, f)
                    # 末根收盘价一致说明复权因子未变，可增量追加
                    _new_close = _ks[_ks["date"] == last_klines.iloc[-1]["date"]].iloc[0]["close"]
                    _old_close = last_klines.iloc[-1]["close"]
                    # C5: 复权因子未变则末根收盘价一致→可增量追加。原用浮点 == 精确比较,
                    # MySQL Float 单精度读回必失真→恒 False→每次对全市场走全删全写。改容差比较
                    # (阈值 5e-3 远小于最小报价变动 0.01、远大于单精度存储噪声)。
                    if abs(float(_new_close) - float(_old_close)) < 5e-3:
                        tqdm.write(f"{code} {f} 增量更新数据：{len(_ks)}")
                        db_ex.insert_klines(db_code, f, _ks)
                        continue
                    tqdm.write(
                        f"{code} {f} 时间 ： {last_klines.iloc[-1]['date']} 原始收盘价: {last_klines.iloc[-1]['close']} 新收盘价: {_ks[_ks['date'] == last_klines.iloc[-1]['date']].iloc[0]['close']}"
                    )
                except Exception as e:
                    print(f"{code} {f} 数据对比异常：{str(e)}，进行全量更新")

            # 全量更新：短周期数据量大，按周期设置不同起始时间以控制存储量
            # C5: 先取数+复权+合成, 成功且非空才 del+insert。原写法先 del_klines_by_code_freq
            # 再取数, 中间任一步(DB断连/gm源异常/复权除零)抛异常或进程被杀→外层 except 仅 print,
            # 该股日线清空到下次 cron 全量自愈, 期间历史选股/回测读空表。
            if f == "5m":
                start_dt = "2021-01-01 00:00:00"
            elif f == "15m":
                start_dt = "2023-01-01 00:00:00"
            elif f == "30m":
                start_dt = "2018-01-01 00:00:00"
            elif f == "60m":
                start_dt = "2015-01-01 00:00:00"
            else:
                start_dt = None
            gm_klines = db_ex.klines(
                code,
                gm_freq,
                start_date=start_dt,
                args={"limit": 9999999},
            )
            gm_klines["volume"] = gm_klines["volume"] / 100  # 掘金成交量单位为手，换算为股
            gm_klines = tdx_ex.klines_fq(gm_klines, xdxr, "qfq")
            _ks = convert_stock_kline_frequency(gm_klines, f)
            if _ks is not None and len(_ks) > 0:
                db_ex.del_klines_by_code_freq(db_code, f)
                db_ex.insert_klines(db_code, f, _ks)
                tqdm.write(f"{code} {f} 全量更新数据：{len(_ks)}")
            else:
                tqdm.write(f"{code} {f} 全量数据为空, 跳过删除保留原库存")

    except Exception as e:
        print(f"Convert {code} error : {str(e)[0:200]}")
    return True


if __name__ == "__main__":
    for _code in tqdm(run_codes, desc="同步进度"):
        sync_code(_code)

    for code in tqdm(run_codes):
        convert_code(code)

    print("Done")
