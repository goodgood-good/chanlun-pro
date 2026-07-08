#:  -*- coding: utf-8 -*-
"""定时同步 Binance 现货数字货币 K 线到本地数据库。"""
from chanlun.exchange.exchange_binance_spot import ExchangeBinanceSpot
from chanlun.exchange.exchange_db import ExchangeDB
import traceback
from tqdm.auto import tqdm

exchange = ExchangeDB("currency_spot")
line_exchange = ExchangeBinanceSpot()

stocks = line_exchange.all_stocks()
codes = [s["code"] for s in stocks]
codes = ["BTC/USDT"]
sync_frequencys = ["w", "d", "4h", "60m", "30m", "15m", "10m", "5m", "1m"]

# 各周期首次全量拉取的起始时间（短周期数据量大，起始时间设得较晚以控制存储量）
f_start_time_maps = {
    "w": "2000-01-01 00:00:00",
    "d": "2000-01-01 00:00:00",
    "4h": "2000-01-01 00:00:00",
    "60m": "2000-01-01 00:00:00",
    "30m": "2020-01-01 00:00:00",
    "15m": "2022-01-01 00:00:00",
    "10m": "2023-01-01 00:00:00",
    "5m": "2024-01-01 00:00:00",
    "1m": "2024-10-01 00:00:00",
}

if __name__ == "__main__":
    for code in tqdm(codes):
        try:
            for f in sync_frequencys:
                while True:
                    try:
                        last_dt = exchange.query_last_datetime(code, f)
                        if last_dt is None:
                            klines = line_exchange.klines(
                                code,
                                f,
                                end_date=f_start_time_maps[f],
                                args={"use_online": True},
                            )
                            if len(klines) == 0:
                                klines = line_exchange.klines(
                                    code,
                                    f,
                                    start_date=f_start_time_maps[f],
                                    args={"use_online": True},
                                )
                        else:
                            klines = line_exchange.klines(
                                code, f, start_date=last_dt, args={"use_online": True}
                            )

                        tqdm.write(
                            "Run code %s frequency %s klines len %s"
                            % (code, f, len(klines))
                        )
                        exchange.insert_klines(code, f, klines)
                        if len(klines) <= 1:
                            break
                    except Exception:
                        tqdm.write("执行 %s 同步K线异常" % code)
                        tqdm.write(traceback.format_exc())
                        break

        except Exception as e:
            tqdm.write("执行 %s 同步K线异常" % code)
            tqdm.write(e)
            tqdm.write(traceback.format_exc())
