#:  -*- coding: utf-8 -*-
"""数字货币（Binance）自动化交易启动脚本，每 5 分钟触发一次策略执行。"""
import time
import traceback

from chanlun import fun
from chanlun.core.types import Config
from chanlun.exchange.exchange_binance import ExchangeBinance
from chanlun.strategy.strategy_demo import StrategyDemo
from chanlun.trader.online_market_datas import OnlineMarketDatas
from chanlun.trader.trader_currency import TraderCurrency

logger = fun.get_logger("trader_currency.log")

logger.info("数字货币自动化交易程序")

try:
    ex = ExchangeBinance()
    run_num = 30
    # 初始取 24h 交易量排行前 30 个品种，每小时动态刷新
    run_codes = ex.ticker24HrRank(run_num)
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
        "xd_qj": Config.XD_QJ_DD.value,
        # 中枢默认配置
        "zs_bi_type": Config.ZS_TYPE_DN.value,  # 笔中枢类型
        "zs_xd_type": Config.ZS_TYPE_DN.value,  # 走势中枢类型
        "zs_qj": Config.ZS_QJ_CK.value,
        "zs_wzgx": Config.ZS_WZGX_ZGD.value,
    }

    p_redis_key = "trader_currency"

    TR = TraderCurrency("Currency", log=logger.info)
    TR.load_from_pkl(p_redis_key)
    # 审计 D1-HIGH-4: 启动对账——以券商持仓为准核对本地, 不一致仅告警(疑已成交未落盘/误持),
    # 不自动改仓。修复 reconcile_positions 此前为死代码(从未被调用)。
    try:
        TR.reconcile_positions(list(set(run_codes + TR.position_codes())))
    except Exception:
        logger.error(f"启动对账异常: {traceback.format_exc()}")
    Data = OnlineMarketDatas("currency", frequencys, ex, cl_config)
    STR = StrategyDemo()
    TR.set_strategy(STR)
    TR.set_data(Data)

    logger.info("Run symbols: %s" % run_codes)

    while True:
        try:
            seconds = int(time.time())

            if seconds % (60 * 60) == 0:
                # 每小时更新 24h 交易量排行，动态调整监控品种
                run_codes = ex.ticker24HrRank(run_num)
                logger.info("Run symbols: %s" % run_codes)

            if seconds % (5 * 60) != 0:
                time.sleep(1)
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
