from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
import pickle
import traceback

import talib

from chanlun import fun
from chanlun.trading.backtest_klines import BackTestKlines
from chanlun.trading.base import Strategy
from chanlun.core.types import BI, ICL
from chanlun.cl_utils import (
    query_cl_chart_config,
)
from chanlun.persistence.db import db
from chanlun.zixuan import ZiXuan
from chanlun.config import get_data_path
from tqdm.auto import tqdm
from chanlun.core import cl

"""
历史选股脚本：回放历史行情验证选股条件的准确率。

前提：数据库中需要有对应标的的历史行情数据。
"""


class HistoryXuangu(object):

    def __init__(self):
        self.market = "a"
        self.xg_start_date = "2022-01-01 00:00:00"
        self.xg_end_date = "2025-01-01 00:00:00"
        self.freqencys = ["30m"]
        self.cl_config = query_cl_chart_config(self.market, "SH.000001")

        self.zx = ZiXuan(self.market)
        self.zx_group = "测试选股"

        # 选股结果 pkl 的输出目录，不存在则自动创建
        self.xg_result_path = get_data_path() / "history_xuangu"
        if not self.xg_result_path.exists():
            self.xg_result_path.mkdir(parents=True, exist_ok=True)

    def clear_zx_mark(self):
        """清空本次选股结果：删除自选组中的股票及图表上的标记点。"""
        self.zx.clear_zx_stocks(self.zx_group)
        db.marks_del(self.market, "XG")
        return True

    def xuangu_by_code(self, code: str):
        """
        给定一个股票代码，执行该股票的历史选股
        """

        bk = BackTestKlines(
            self.market,
            start_date=self.xg_start_date,
            end_date=self.xg_end_date,
            frequencys=self.freqencys,
            cl_config=self.cl_config,
        )
        bk.init(code, self.freqencys[-1])

        xg_res = []

        # 预先计算全量缠论数据，用于事后验证选股点笔是否真实结束
        all_klines = bk.ex.klines(
            code, self.freqencys[0], end_date=self.xg_end_date, args={"limit": 20000}
        )
        all_cd: ICL = cl.CL(
            code, self.freqencys[0], config=self.cl_config
        ).process_klines(all_klines)

        while bk.next():
            try:
                klines = bk.klines(code, frequency=bk.frequencys[0])
                if len(klines) <= 100:
                    continue
                cd = bk.get_cl_data(code, bk.frequencys[0])
                if len(cd.get_bis()) == 0:  # 没有笔，跳过
                    continue
                bi = cd.get_bis()[-1]
                if bi.type == "up":  # 当前笔向上，不符合买点方向
                    continue

                if not bi.is_done():
                    continue

                k = cd.get_src_klines()[-1]
                if k.c < k.o:  # 当前 K 线收阴，不符合
                    continue

                # 当前价格须在最近笔中枢下轨以下，确保处于超卖区间
                last_zs = cd.get_last_bi_zs()
                if last_zs is None:
                    continue
                if k.h >= last_zs.zd:
                    continue

                # 成交量须高于 10 日均量，量能放大佐证反转
                idx_volume_ma_10 = talib.MA(klines["volume"].to_numpy(), timeperiod=10)
                if k.a < idx_volume_ma_10[-1]:
                    continue

                # 截取笔起点以来的指标数据
                cal_bi_start_i = cd.get_src_klines()[-1].index - bi.start.k.k_index

                # KDJ 背离判断：一笔内 J 线至少两次向上转折，且后高于前（超卖背驰）
                idx_kdj = Strategy.idx_kdj(cd, 9, 3, 3)
                idx_kdj_ks = idx_kdj["k"][-cal_bi_start_i:]
                idx_kdj_ds = idx_kdj["d"][-cal_bi_start_i:]
                idx_kdj_js = idx_kdj["j"][-cal_bi_start_i:]

                # J 线向上转折点，前后至少间隔 5 根 K 线（需三点关系确认转折）
                idx_kdj_zz_ji = []
                for i in range(2, len(idx_kdj_js)):
                    if (
                        idx_kdj_js[i] > idx_kdj_js[i - 1]
                        and idx_kdj_js[i - 1] < idx_kdj_js[i - 2]
                        and (len(idx_kdj_zz_ji) == 0 or i - idx_kdj_zz_ji[-1] > 5)
                    ):
                        idx_kdj_zz_ji.append(i - 1)
                if len(idx_kdj_zz_ji) < 2:
                    continue
                # 后一次转折 J 值高于前一次，形成背驰
                if idx_kdj_js[idx_kdj_zz_ji[-1]] <= idx_kdj_js[idx_kdj_zz_ji[-2]]:
                    continue
                # D 值最低点须低于 20，确认超卖
                if min([idx_kdj_ds[_i] for _i in idx_kdj_zz_ji]) > 20:
                    continue

                # KDJ 金叉判断
                judge_kdj_gold = False
                if idx_kdj_ks[-1] > idx_kdj_ds[-1] and idx_kdj_ks[-2] < idx_kdj_ds[-2]:
                    judge_kdj_gold = True

                # MACD 金叉判断
                idx_macd = Strategy.idx_macd(cd, 6, 13, 5)
                judge_macd_gold = False
                if (
                    idx_macd["dif"][-1] > idx_macd["dea"][-1]
                    and idx_macd["dif"][-2] < idx_macd["dea"][-2]
                ):
                    judge_macd_gold = True

                # KDJ 或 MACD 至少一个金叉才触发选股
                if judge_kdj_gold is False and judge_macd_gold is False:
                    continue

                # 记录价格与均线的位置关系，供事后统计分析
                idx_ma_5 = Strategy.idx_ma(cd, 5)
                idx_ma_5_close = k.c < idx_ma_5[-1]
                idx_ma_10 = Strategy.idx_ma(cd, 10)
                idx_ma_10_close = k.c < idx_ma_10[-1]
                idx_ma_20 = Strategy.idx_ma(cd, 20)
                idx_ma_20_close = k.c < idx_ma_20[-1]

                # 用全量缠论数据验证：选股点的笔是否真正结束
                bi_end_success = False
                bi_next_bi: BI = None
                for _bi in all_cd.get_bis():
                    if _bi.end.k.date == bi.end.k.date:
                        bi_end_success = True
                        bi_next_bi = all_cd.get_bis()[_bi.index + 1]
                        break

                # 验证下一笔最高点是否突破中枢下轨（涨幅充分）
                up_zd_success = False
                if bi_end_success:
                    all_bis = all_cd.get_bis()
                    while True:
                        if bi_next_bi.index + 2 >= len(all_bis):
                            break
                        if (
                            all_bis[bi_next_bi.index + 2].high > bi_next_bi.high
                            and all_bis[bi_next_bi.index + 2].low > bi_next_bi.low
                        ):
                            bi_next_bi = all_bis[bi_next_bi.index + 2]
                        else:
                            break
                    xg_next_bi_high = bi_next_bi.high
                    if xg_next_bi_high > last_zs.zd:
                        up_zd_success = True

                xg_res.append(
                    {
                        "code": code,
                        "xg_date": cd.get_src_klines()[-1].date,
                        "kdj_j_zz_num": len(idx_kdj_zz_ji),
                        "kdj_j_zz_vals": [
                            {"i": _i, "v": idx_kdj_js[_i]} for _i in idx_kdj_zz_ji
                        ],
                        "kdj_k_zz_vals": [
                            {"i": _i, "v": idx_kdj_ks[_i]} for _i in idx_kdj_zz_ji
                        ],
                        "kdj_d_zz_vals": [
                            {"i": _i, "v": idx_kdj_ds[_i]} for _i in idx_kdj_zz_ji
                        ],
                        "macd_dif": idx_macd["dif"][-1],
                        "macd_dea": idx_macd["dea"][-1],
                        "kdj_k": idx_kdj_ks[-1],
                        "kdj_d": idx_kdj_ds[-1],
                        "judge_kdj_gold": judge_kdj_gold,
                        "judge_macd_gold": judge_macd_gold,
                        "idx_ma_5_close": idx_ma_5_close,
                        "idx_ma_10_close": idx_ma_10_close,
                        "idx_ma_20_close": idx_ma_20_close,
                        "pre_bi_mmds": "/".join(cd.get_bis()[-2].line_mmds("|")),
                        "last_zs_line_nums": last_zs.line_num,
                        "last_zs_type": last_zs.type,
                        "last_zs_direction": last_zs.lines[0].type,
                        "bi_end_success": bi_end_success,
                        "up_zd_success": up_zd_success,
                        "xg_next_high": bi_next_bi.high if bi_next_bi else 0,
                    }
                )
                self.zx.add_stock(self.zx_group, code, None)
                db.marks_add(
                    self.market,
                    code,
                    "",
                    "",
                    fun.datetime_to_int(cd.get_src_klines()[-1].date),
                    "XG",
                    f"KDJ背离，MACD死叉，预示笔的结束 [k:{idx_kdj_ks[-1]:.4f} d:{idx_kdj_ds[-1]:.4f} macd dif:{idx_macd['dif'][-1]:.4f} macd dea: {idx_macd['dea'][-1]:.4f}]",
                    "earningUp",
                    "red",
                )
                tqdm.write(
                    f"{code} - {cd.get_src_klines()[-1].date} 符合选股 笔结束 {bi_end_success} 笔后涨幅超过zd {up_zd_success}"
                )

            except Exception as e:
                print(f"{code} 选股异常")
                print(traceback.format_exc())

        with open(self.xg_result_path / f"xg_{code}.pkl", "wb") as fp:
            pickle.dump(xg_res, fp)

        return True


if __name__ == "__main__":

    from chanlun.exchange.exchange_tdx import ExchangeTDX

    ex = ExchangeTDX()
    stocks = ex.all_stocks()
    run_codes = [
        _s["code"]
        for _s in stocks
        if _s["code"][0:5] in ["SH.60", "SZ.00", "SZ.30"] and "ST" not in _s["name"]
    ]
    print(f"选股股票数量 {len(run_codes)}")

    hxg = HistoryXuangu()

    print("开始选股")
    print(f"{hxg.xg_start_date} ~ {hxg.xg_end_date}")

    # 多进程执行选股，max_workers 根据 CPU 核数调整
    with ProcessPoolExecutor(
        max_workers=24, mp_context=get_context("spawn")
    ) as executor:
        bar = tqdm(total=len(run_codes))
        for _ in executor.map(
            hxg.xuangu_by_code,
            run_codes,
        ):
            bar.update(1)

    print("Done")
