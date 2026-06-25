"""期货 CTP 自动化交易启动脚本，每 5 分钟触发一次策略执行，含止损与持仓时间风控。"""
import time
import traceback

from chanlun import fun
from chanlun.core.types import Config
from chanlun.exchange.exchange_ctp import MarketCTP
from chanlun.strategy.strategy_demo import StrategyDemo
from chanlun.trader.online_market_datas import OnlineMarketDatas
from chanlun.trader.trader_ctp import CTPTrader
from chanlun.trading.base import Operation

logger = fun.get_logger("trader_ctp.log")

logger.info("期货自动化交易程序")

try:
    market = MarketCTP()

    run_codes = ["rb2405", "IF2403", "IC2403", "au2406"]  # 按需修改交易品种
    frequencys = ["15m", "5m"]

    cl_config = {
        "fx_qj": Config.FX_QJ_K.value,
        "fx_bh": Config.FX_BH_YES.value,
        "bi_type": Config.BI_TYPE_NEW.value,
        "bi_bzh": Config.BI_BZH_YES.value,
        "bi_fx_cgd": Config.BI_FX_CHD_NO.value,
        "bi_qj": Config.BI_QJ_DD.value,
        "zs_bi_type": Config.ZS_TYPE_DN.value,
        "zs_seg_type": Config.ZS_TYPE_DN.value,
        "zs_qj": Config.ZS_QJ_CK.value,
        "zs_wzgx": Config.ZS_WZGX_ZGD.value,
    }

    p_strategy_key = "trader_ctp"

    TR = CTPTrader("CTP", log=logger.info)
    TR.load_from_pkl(p_strategy_key)
    Data = OnlineMarketDatas("futures", frequencys, market, cl_config)
    STR = StrategyDemo()
    TR.set_strategy(STR)
    TR.set_data(Data)

    logger.info(f"交易品种: {run_codes}")

    while True:
        try:
            # 判断是否是交易时间
            if not market.now_trading():
                time.sleep(10)
                continue

            seconds = int(time.time())

            # 每5分钟运行一次策略
            if seconds % (5 * 60) != 0:
                time.sleep(1)
                continue

            # 持仓中的标的也纳入本轮执行
            run_codes = TR.position_codes() + run_codes
            run_codes = list(set(run_codes))

            for code in run_codes:
                try:
                    TR.run(code)
                except Exception:
                    logger.error(f"{code} 策略运行异常: {traceback.format_exc()}")
                finally:
                    # H3-a: 每个 code 处理后立即落盘, 把"已下单未持久化"崩溃窗口
                    # 从整轮缩到单 code; 落盘失败不应中断后续 code 处理。
                    try:
                        TR.save_to_pkl(p_strategy_key)
                    except Exception:
                        logger.error(f"{code} 落盘失败: {traceback.format_exc()}")

            Data.clear_cache()
            TR.save_to_pkl(p_strategy_key)  # 轮末再保险落一次

            # 风控检查：止损 + 持仓时间超限强平
            # 注: force_close 第三参须为 Operation (内部读 opt.msg), 不能传裸 str
            positions = TR.get_positions()
            for pos in positions:
                # 检查止损
                if TR.check_stop_loss(pos):
                    TR.force_close(
                        pos.code,
                        pos,
                        Operation(pos.code, "close", "risk", msg="触发止损"),
                    )
                # 检查持仓时间 (与止损 if/elif: 同一仓已强平就别再因时间重复发第二笔平单)
                elif TR.check_position_time(pos):
                    TR.force_close(
                        pos.code,
                        pos,
                        Operation(pos.code, "close", "risk", msg="持仓时间过长"),
                    )

        except Exception:
            logger.error(f"主循环异常: {traceback.format_exc()}")
            time.sleep(10)

except Exception:
    logger.error(f"程序异常退出: {traceback.format_exc()}")
finally:
    logger.info("程序结束")
    if "TR" in locals():
        TR.close()
