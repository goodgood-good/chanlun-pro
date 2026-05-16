#:  -*- coding: utf-8 -*-
"""港股自动化交易启动脚本，每 5 分钟触发一次策略执行。"""
import time
import traceback

from chanlun import fun
from chanlun.core.cl_interface import Config
from chanlun.exchange.exchange_futu import ExchangeFutu
from chanlun.strategy.strategy_demo import StrategyDemo
from chanlun.trader.online_market_datas import OnlineMarketDatas
from chanlun.trader.trader_hk_stock import TraderHKStock

logger = fun.get_logger("trader_hk_stock.log")

logger.info("港股自动化交易程序")

G_Trade_days = None  # 交易日历缓存，供扩展用

ex = ExchangeFutu()

try:
    run_codes = [
        "HK.00189",
        "HK.01072",
        "HK.09868",
        "HK.02150",
        "HK.06699",
        "HK.02618",
        "HK.01789",
        "HK.06127",
        "HK.09961",
        "HK.02400",
        "HK.09626",
        "HK.01579",
        "HK.09888",
        "HK.00772",
        "HK.00520",
        "HK.06186",
        "HK.01024",
        "HK.06098",
        "HK.09983",
        "HK.09618",
        "HK.01810",
        "HK.03690",
        "HK.00700",
        "HK.00268",
        "HK.02013",
        "HK.09922",
        "HK.02238",
        "HK.00285",
        "HK.01797",
        "HK.00981",
        "HK.00763",
        "HK.06969",
        "HK.06862",
        "HK.09999",
        "HK.09988",
        "HK.01211",
        "HK.09633",
        "HK.00388",
        "HK.02269",
        "HK.02020",
        "HK.06690",
        "HK.01876",
        "HK.00175",
        "HK.09901",
        "HK.00291",
        "HK.02382",
        "HK.09987",
        "HK.03606",
        "HK.02331",
        "HK.09992",
        "HK.00853",
        "HK.00322",
        "HK.06060",
        "HK.01951",
        "HK.01458",
        "HK.01928",
        "HK.03318",
    ]
    frequencys = ["30m"]

    cl_config = {
        # 分型默认配置
        "fx_qj": Config.FX_QJ_K.value,
        "fx_bh": Config.FX_BH_YES.value,
        # 笔默认配置
        "bi_type": Config.BI_TYPE_NEW.value,
        "bi_bzh": Config.BI_BZH_YES.value,
        "bi_fx_cgd": Config.BI_FX_CHD_NO.value,
        "bi_qj": Config.BI_QJ_DD.value,
        # 线段默认配置
        "xd_bzh": Config.XD_BZH_NO.value,
        "xd_qj": Config.XD_QJ_DD.value,
        # 中枢默认配置
        "zs_bi_type": Config.ZS_TYPE_DN.value,  # 笔中枢类型
        "zs_xd_type": Config.ZS_TYPE_DN.value,  # 走势中枢类型
        "zs_qj": Config.ZS_QJ_CK.value,
        "zs_wzgx": Config.ZS_WZGX_ZGD.value,
    }

    p_redis_key = "trader_hk_stock"

    TR = TraderHKStock("hk", log=logger.info)
    TR.load_from_pkl(p_redis_key)
    Data = OnlineMarketDatas("hk", frequencys, ex, cl_config)
    STR = StrategyDemo()
    TR.set_strategy(STR)
    TR.set_data(Data)

    while True:
        try:
            seconds = int(time.time())

            if seconds % (5 * 60) != 0:
                time.sleep(1)
                continue

            if ex.now_trading() is False:
                continue

            # 持仓中的标的也纳入本轮执行
            run_codes = TR.position_codes() + run_codes
            run_codes = list(set(run_codes))

            for code in run_codes:
                try:
                    TR.run(code)
                except Exception as e:
                    logger.error(traceback.format_exc())

            # 每轮结束后清空 K 线缓存，确保下轮能拉到最新数据
            Data.clear_cache()
            TR.save_to_pkl(p_redis_key)

        except Exception as e:
            logger.error(traceback.format_exc())

except Exception as e:
    logger.error(traceback.format_exc())

finally:
    logger.info("Done")
