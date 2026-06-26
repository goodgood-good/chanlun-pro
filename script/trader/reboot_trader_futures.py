#:  -*- coding: utf-8 -*-
"""期货自动化交易启动脚本:加载策略与天勤行情,循环驱动 TraderFutures 执行交易。"""
import time
import traceback

from chanlun import fun, zixuan
from chanlun.core.types import Config
from chanlun.exchange.exchange_tq import ExchangeTq
from chanlun.strategy import strategy_demo
from chanlun.trader.online_market_datas import OnlineMarketDatas
from chanlun.trader.trader_futures import TraderFutures

logger = fun.get_logger("trader_futures.log")

logger.info("期货自动化交易程序")

try:
    zx = zixuan.ZiXuan("futures")
    ex = ExchangeTq(use_account=True)
    frequencys = ["10s"]
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
        # 走势类型默认配置
        "zslx_bzh": Config.ZSLX_BZH_NO.value,
        "zslx_qj": Config.ZSLX_QJ_DD.value,
        # 中枢默认配置
        "zs_bi_type": Config.ZS_TYPE_DN.value,  # 笔中枢类型
        "zs_xd_type": Config.ZS_TYPE_DN.value,  # 走势中枢类型
        "zs_qj": Config.ZS_QJ_CK.value,
        "zs_wzgx": Config.ZS_WZGX_ZGD.value,
    }

    p_redis_key = "trader_futures"

    TR = TraderFutures("futures", log=logger.info)
    # 进程重启后从 pkl 恢复持仓与交易状态
    TR.load_from_pkl(p_redis_key)
    # 审计 D1-HIGH-4: 启动对账——以券商持仓为准核对本地持仓, 不一致仅告警(疑已成交未落盘/误持),
    # 不自动改仓。修复 reconcile_positions 此前为死代码。注: run_codes 在循环内由选股动态产生,
    # 启动仅对账本地已持仓 code(broker-has-local-none 由循环内每轮 run 自然覆盖)。
    try:
        TR.reconcile_positions(TR.position_codes())
    except Exception:
        logger.error(f"启动对账异常: {traceback.format_exc()}")
    Data = OnlineMarketDatas("futures", frequencys, ex, cl_config)
    STR = strategy_demo.StrategyDemo()

    TR.set_strategy(STR)
    TR.set_data(Data)

    while True:
        try:
            seconds = int(time.time())

            # 每 10 秒检查一次行情,其余时间休眠让出 CPU
            if seconds % (10) != 0:
                time.sleep(1)
                continue

            if ex.now_trading() is False:
                continue

            # 合并"我的持仓"自选组与交易器已有持仓,作为本轮要驱动的标的
            stocks = zx.zx_stocks("我的持仓")
            run_codes = [_s["code"] for _s in stocks]
            run_codes = TR.position_codes() + run_codes
            run_codes = list(set(run_codes))

            for code in run_codes:
                try:
                    TR.run(code)
                except Exception as e:
                    logger.error(traceback.format_exc())

            # 清空之前获取的k线缓存，避免后续无法获取最新数据
            Data.clear_cache()
            # 保存交易数据到 Redis 中
            TR.save_to_pkl(p_redis_key)

        except Exception as e:
            logger.error(traceback.format_exc())

except Exception as e:
    logger.error(traceback.format_exc())
finally:
    logger.info("Done")
