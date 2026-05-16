#:  -*- coding: utf-8 -*-
"""
沪深 A 股多进程选股模板。

请勿直接修改此文件，建议 copy 后重命名，在副本中修改选股逻辑并运行。
"""
import time
import traceback
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context

from tqdm.auto import tqdm

from chanlun import zixuan
from chanlun.cl_utils import query_cl_chart_config
from chanlun.exchange.exchange_tdx import ExchangeTDX
from chanlun.trader.online_market_datas import OnlineMarketDatas
from chanlun.xuangu import xuangu

ex = ExchangeTDX()

# 选股周期，根据选股方法按需调整
frequencys = ["d"]

# 缠论配置须与前台展示一致，否则选股结果在图表上可能看不到对应形态
cl_config = query_cl_chart_config("a", "SH.000001")

# 选股无需使用缓存，开启缓存会占用大量内存
mk_datas = OnlineMarketDatas(
    "a", frequencys, ex, cl_config, use_cache=False
)

zx = zixuan.ZiXuan("a")
zx_group = "测试选股"


def xuangu_by_code(code: str):
    """对单只股票执行选股条件判断，符合条件则写入自选组。"""
    try:
        # 在此填入选股条件，可调用 xuangu 模块中的方法或自行实现
        xg_res = xuangu.xg_single_xd_zs_nei_3mmds(code, mk_datas, opt_type=["long"])
        if xg_res is not None:
            stocks = ex.stock_info(code)
            tqdm.write(
                "【%s - %s 】 出现机会：%s"
                % (stocks["code"], stocks["name"], xg_res["msg"])
            )
            zx.add_stock(zx_group, stocks["code"], stocks["name"])
    except Exception as e:
        print("Code : %s Run Exception : %s" % (code, e))
        traceback.print_exc()


if __name__ == "__main__":
    _stime = time.time()
    # 多进程选股，max_workers 不宜过大以免触发 TDX 频率限制
    with ProcessPoolExecutor(
        max_workers=5, mp_context=get_context("spawn")
    ) as executor:
        codes = ex.all_stocks()
        codes = [
            _s["code"] for _s in codes if _s["code"][0:5] in ["SH.60", "SZ.00", "SZ.30"]
        ]
        print("运行股票数量：", len(codes))

        zx.clear_zx_stocks(zx_group)

        bar = tqdm(total=len(codes), desc="选股进度")
        for _r in executor.map(xuangu_by_code, codes):
            bar.update(1)

    print("运行时间：%s" % (time.time() - _stime))
    print("Done")
