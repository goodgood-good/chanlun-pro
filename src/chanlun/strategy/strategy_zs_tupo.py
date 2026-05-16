from typing import List, Union

import numpy as np
import talib as ta

from chanlun.backtesting.base import POSITION, Dict, MarketDatas, Operation, Strategy


class StrategyZSTupo(Strategy):
    """
    仿 https://www.bilibili.com/video/BV1Uo4y1v7RL/ 策略，做中枢突破

    适合日线及以上大周期的策略

    两个级别（日线、30分钟）

    中枢使用 30分钟的线段中枢
    """

    def __init__(
        self, pre_zf_rate=None, zs_zd_days=None, ema_params=None, max_loss_rate=None
    ):
        super().__init__()

        # 中枢之前的振幅比例在 一下区间（单位百分比）
        self.pre_zf_rate = [30, 100] if pre_zf_rate is None else pre_zf_rate
        # 中枢震荡的时间范围（天数）
        self.zs_zd_days = [14, 80] if zs_zd_days is None else zs_zd_days

        # EMA 参数 (由小到大)
        self.ema_params = [10, 20, 50] if ema_params is None else ema_params

        # 最大亏损比例
        self.max_loss_rate = 5 if max_loss_rate is None else max_loss_rate

    def open(
        self, code, market_data: MarketDatas, poss: Dict[str, POSITION]
    ) -> List[Operation]:
        """
        开仓监控，返回开仓配置
        """
        opts = []

        cd_lv0 = market_data.get_cl_data(code, market_data.frequencys[1])
        cd_lv1 = market_data.get_cl_data(code, market_data.frequencys[0])
        if len(cd_lv0.get_xd_zss()) == 0:
            return opts

        bi_lv1 = cd_lv1.get_bis()[-1]
        xd_lv0 = cd_lv0.get_xds()[-1]
        xd_zs_lv0 = cd_lv0.get_xd_zss()[-1]
        # 最新线段须是中枢最后一条线段，确保中枢仍在震荡中（未突破）
        if xd_lv0.index != xd_zs_lv0.lines[-1].index:
            return opts

        # 用日线 K 线数量估算中枢震荡天数（排除节假日影响）
        zd_days = len(
            [_k for _k in cd_lv1.get_klines() if xd_zs_lv0.start.k.date <= _k.date]
        )
        if (self.zs_zd_days[0] <= zd_days <= self.zs_zd_days[1]) is False:
            return opts

        # 计算进入中枢前那段趋势的涨/跌幅；前趋势振幅过小说明中枢缺乏动能
        into_zs_xd_lv0 = xd_zs_lv0.lines[0]
        if into_zs_xd_lv0.type == "up":
            # 进入线段向上：向上做多；若前一同向线段低点更低，以更早的线段作为起点计算涨幅
            opt_direction = "buy"
            start_up_xd_lv0 = into_zs_xd_lv0
            if (
                start_up_xd_lv0.index >= 2
                and cd_lv0.get_xds()[start_up_xd_lv0.index - 2].low
                < start_up_xd_lv0.low
            ):
                start_up_xd_lv0 = cd_lv0.get_xds()[start_up_xd_lv0.index - 2]
            zf_rate = abs(
                (into_zs_xd_lv0.high - start_up_xd_lv0.low) / start_up_xd_lv0.low * 100
            )
        else:
            # 进入线段向下：向下做空；若前一同向线段低点更低，以更早的线段作为起点计算跌幅
            opt_direction = "sell"
            start_up_xd_lv0 = into_zs_xd_lv0
            if (
                start_up_xd_lv0.index >= 2
                and cd_lv0.get_xds()[start_up_xd_lv0.index - 2].low
                < start_up_xd_lv0.low
            ):
                start_up_xd_lv0 = cd_lv0.get_xds()[start_up_xd_lv0.index - 2]
            zf_rate = abs(
                (into_zs_xd_lv0.low - start_up_xd_lv0.high) / start_up_xd_lv0.high * 100
            )

        if (self.pre_zf_rate[0] <= zf_rate <= self.pre_zf_rate[1]) is False:
            return opts

        # EMA 排列确认趋势方向：多头排列才做多，空头排列才做空
        if opt_direction == "buy":
            # EMA 多头排列（短周期 > 中周期 > 长周期）
            klines_lv1 = [
                _k for _k in cd_lv1.get_klines() if _k.date <= into_zs_xd_lv0.end.k.date
            ]
            kline_closes = np.array(
                [_k.c for _k in klines_lv1][-(max(self.ema_params) * 2) :]
            )
            ema_idx = []
            for _p in self.ema_params:
                ema_idx.append(ta.EMA(kline_closes, _p))
            if (ema_idx[0][-1] > ema_idx[1][-1] > ema_idx[2][-1]) is False:
                return opts
        else:
            # EMA 空头排列（短周期 < 中周期 < 长周期）
            klines_lv1 = [
                _k for _k in cd_lv1.get_klines() if _k.date <= into_zs_xd_lv0.end.k.date
            ]
            kline_closes = np.array(
                [_k.c for _k in klines_lv1][-(max(self.ema_params) * 2) :]
            )
            ema_idx = []
            for _p in self.ema_params:
                ema_idx.append(ta.EMA(kline_closes, _p))
            if (ema_idx[0][-1] < ema_idx[1][-1] < ema_idx[2][-1]) is False:
                return opts

        kline_lv1 = cd_lv1.get_klines()[-1]
        kline_lv0 = cd_lv0.get_klines()[-1]

        # 验证突破是否是刚发生的：前两根 K 线须有一根价格仍在中枢内，防止平仓后在中枢外重复开仓
        kline_p1_lv1 = cd_lv1.get_klines()[-2]
        kline_p2_lv1 = cd_lv1.get_klines()[-3]

        # 记录一些信息
        info = {
            "zf_rate": zf_rate,
            "zd_days": zd_days,
            "zs_zf": xd_zs_lv0.zf(),
            "zs_lines": len(xd_zs_lv0.lines),
            "bi_lv1_mmd_bc": "/".join(bi_lv1.line_mmds() + bi_lv1.line_bcs()),
            "zx_into_line_mmd_bc": "/".join(
                xd_zs_lv0.lines[0].line_mmds() + xd_zs_lv0.lines[0].line_bcs()
            ),
            "open_date": kline_lv1.date,
        }

        # 做多开仓：收盘突破中枢 zg，价格站上 EMA 最短周期，当日阳线，且前两根有 K 线仍在中枢内（刚突破）
        if (
            opt_direction == "buy"
            and kline_lv1.c > ema_idx[0][-1]
            and (kline_p1_lv1.l < xd_zs_lv0.zg or kline_p2_lv1.l < xd_zs_lv0.zg)
            and xd_zs_lv0.zg < kline_lv1.c
            and kline_lv1.c > kline_lv1.o
        ):
            loss_price = kline_lv1.l
            open_pos_rate = self.get_open_pos_rate(
                self.max_loss_rate, kline_lv1.c, loss_price
            )
            info["open_pos_rate"] = open_pos_rate  # 记录开仓仓位比例，供平仓分批使用
            opts.append(
                Operation(
                    code,
                    "buy",
                    "3buy",
                    loss_price,
                    info,
                    f"价格突破 zg {xd_zs_lv0.zg}，做多买入 {open_pos_rate}",
                    pos_rate=open_pos_rate,
                )
            )
        # 做空开仓：收盘跌破中枢 zd，价格低于 EMA 最短周期，当日阴线，且前两根有 K 线仍在中枢内（刚突破）
        if (
            opt_direction == "sell"
            and kline_lv1.c < ema_idx[0][-1]
            and (kline_p1_lv1.h > xd_zs_lv0.zd or kline_p2_lv1.h > xd_zs_lv0.zd)
            and xd_zs_lv0.zd > kline_lv1.c
            and kline_lv1.c < kline_lv1.o
        ):
            loss_price = kline_p1_lv1.h
            open_pos_rate = self.get_open_pos_rate(
                self.max_loss_rate, kline_lv1.c, loss_price
            )
            info["open_pos_rate"] = open_pos_rate  # 记录开仓占比
            opts.append(
                Operation(
                    code,
                    "buy",
                    "3sell",
                    loss_price,
                    info,
                    f"价格突破 zd {xd_zs_lv0.zd}，做空卖出 {open_pos_rate}",
                    pos_rate=open_pos_rate,
                )
            )

        return opts

    def close(
        self, code, mmd: str, pos: POSITION, market_data: MarketDatas
    ) -> Union[Operation, None, List[Operation]]:
        """
        持仓监控，返回平仓配置
        """
        opts = []
        if pos.balance == 0:
            return None

        cd_lv1 = market_data.get_cl_data(code, market_data.frequencys[0])
        cd_lv0 = market_data.get_cl_data(code, market_data.frequencys[1])
        price = cd_lv0.get_src_klines()[-1].c
        loss_opt = self.check_loss(mmd, pos, price)
        if loss_opt is not None:
            return loss_opt

        kline_lv1 = cd_lv1.get_klines()[-1]
        kline_lv0 = cd_lv0.get_klines()[-1]

        # 开仓后第 5 个交易日触发分批减仓：用日线 K 线计数规避节假日误差
        open_days = len(
            [_k for _k in cd_lv1.get_klines() if _k.date > pos.info["open_date"]]
        )
        if open_days >= 5:
            if ("buy" in mmd and kline_lv1.c < kline_lv1.o) or (
                "sell" in mmd and kline_lv1.c > kline_lv1.o
            ):
                opts.append(
                    Operation(
                        code,
                        "sell",
                        mmd,
                        msg=f'开仓后 5 日，进行平仓三分之一 ({pos.info["open_date"]} / {kline_lv1.date} [{open_days}])',
                        pos_rate=round(pos.info["open_pos_rate"] * 0.3, 2),
                        key="5",  # key 唯一，防止同一批次重复触发
                    )
                )
            # 5 日后将止损上移至入场价，锁定保本，消除持仓风险
            pos.loss_price = pos.price

        # 开仓 5 日内给予趋势发展空间，不提前用 EMA 干预
        if open_days < 5:
            return opts

        kline_closes = np.array([_k.c for _k in cd_lv1.get_klines()][-100:])

        # 做多持仓跌破 EMA10（短均线），趋势可能转弱，先减仓三分之一
        ema_idx_10 = ta.EMA(kline_closes, 10)
        if "buy" in mmd and kline_lv1.c < ema_idx_10[-1] and kline_lv1.c < kline_lv1.o:
            opts.append(
                Operation(
                    code,
                    "sell",
                    mmd,
                    msg=f"价格下穿EMA10均线，平仓三分之一（{kline_lv1.c} < {ema_idx_10[-1]}）",
                    pos_rate=round(pos.info["open_pos_rate"] * 0.3, 2),
                    key="10",
                )
            )
        # 做空持仓上穿 EMA10，趋势可能转强，先减仓三分之一
        if "sell" in mmd and kline_lv1.c > ema_idx_10[-1] and kline_lv1.c > kline_lv1.o:
            opts.append(
                Operation(
                    code,
                    "sell",
                    mmd,
                    msg=f"价格上穿EMA10均线，平仓三分之一（{kline_lv1.c} > {ema_idx_10[-1]}）",
                    pos_rate=round(pos.info["open_pos_rate"] * 0.3, 2),
                    key="10",
                )
            )

        # 做多持仓跌破 EMA20（中均线），趋势确认转弱，全仓平仓
        ema_idx_20 = ta.EMA(kline_closes, 20)
        if "buy" in mmd and kline_lv1.c < ema_idx_20[-1] and kline_lv1.c < kline_lv1.o:
            opts.append(
                Operation(
                    code,
                    "sell",
                    mmd,
                    msg=f"价格下穿EMA20均线，平仓（{kline_lv1.c} < {ema_idx_20[-1]}）",
                )
            )
        # 做空持仓上穿 EMA20，趋势确认转强，全仓平仓
        if "sell" in mmd and kline_lv1.c > ema_idx_20[-1] and kline_lv1.c > kline_lv1.o:
            opts.append(
                Operation(
                    code,
                    "sell",
                    mmd,
                    msg=f"价格上穿EMA20均线，平仓（{kline_lv1.c} > {ema_idx_20[-1]}）",
                )
            )

        return opts


if __name__ == "__main__":
    from chanlun.backtesting.backtest_klines import BackTestKlines
    from chanlun.cl_utils import query_cl_chart_config

    market = "us"
    freqs = ["d", "60m"]
    code = "DISH"
    start_date = "2019-05-31 00:00:00"
    end_date = "2022-11-04 10:30:00"
    cl_config = query_cl_chart_config(market, code)

    btk = BackTestKlines(market, start_date, end_date, freqs, cl_config)
    btk.init(code, freqs[-1])

    STR = StrategyZSTupo()

    open_res = STR.open(code, btk, {})
    print(open_res)

    # pos = POSITION(code, '3sell', 'sell', 100, 9000, 1000, 0, None, info={
    #     'open_date': fun.str_to_datetime('2022-06-13 08:00:00')
    # })
    # close_res = STR.close(code, pos.mmd, pos, btk)
    # print(close_res)
