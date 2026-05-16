from typing import Dict, List, Union

from chanlun.backtesting.base import POSITION, MarketDatas, Operation, Strategy
from chanlun.cl_interface import BI
from chanlun.cl_utils import cal_zs_macd_infos
from chanlun.config import get_data_path


class StrategyASingleAllMmd(Strategy):
    """
    市场：股票市场
    周期：单周期
    推荐缠论配置：
        笔中枢类型：段内中枢
        走势中枢类型：标准中枢
        中枢位置关系：zggdd 较宽松
    策略：
        根据当前笔在线段中枢的位置，在按照笔的买卖点进行操作

    注意：
        需要有上证指数的数据

    """

    def __init__(self):
        super().__init__()

        self._max_loss_rate = None

    def open(
        self, code, market_data: MarketDatas, poss: Dict[str, POSITION]
    ) -> List[Operation]:
        opts = []

        high_data = market_data.get_cl_data(code, market_data.frequencys[0])
        if (
            len(high_data.get_xds()) == 0
            or len(high_data.get_xd_zss()) == 0
            or len(high_data.get_bi_zss()) == 0
            or len(high_data.get_bis()) == 0
        ):
            return opts

        high_xd_zs = high_data.get_xd_zss()[-1]
        high_bi_zs = high_data.get_bi_zss()[-1]
        high_bi_zs_start_bi: BI = high_bi_zs.lines[1]  # 最后笔中枢的第一进入笔（用于判断类三买卖点）
        high_xd = high_data.get_xds()[-1]
        high_bi = self.last_done_bi(high_data.get_bis())
        high_same_bi = high_data.get_bis()[high_bi.index - 2]  # 最后一笔的前一同向笔
        high_xd_bi_zss = [
            _zs
            for _zs in high_data.get_bi_zss()
            if _zs.start.index > high_xd.start.index
        ]  # 最后线段内包含的所有笔中枢
        price = high_data.get_klines()[-1].c

        # 止损设为笔的极值（笔的底/顶分型低/高点）
        loss_price = high_bi.low if high_bi.type == "down" else high_bi.high

        info = {"high_bi": high_bi}

        zs_macd_infos = cal_zs_macd_infos(high_bi_zs, high_data)

        # 一/二类买卖点（左侧）：须在走势中枢 zg/zd 之外，且 MACD dif 已回拉零轴
        if (
            high_bi.mmd_exists(["1buy"])
            and high_bi.low <= high_xd_zs.dd
            and zs_macd_infos.dif_up_cross_num > 0
            and self.bi_td(high_bi, high_data)
        ):
            opts.append(Operation(code, "buy", "1buy", loss_price, info, "一买"))
        if (
            high_bi.mmd_exists(["1sell"])
            and high_bi.high >= high_xd_zs.gg
            and zs_macd_infos.dif_down_cross_num > 0
            and self.bi_td(high_bi, high_data)
        ):
            opts.append(Operation(code, "buy", "1sell", loss_price, info, "一卖"))

        if (
            high_bi.mmd_exists(["2buy"])
            and high_xd_zs.zd > high_bi.low > high_same_bi.low
            and zs_macd_infos.dif_up_cross_num > 0
            and self.bi_td(high_bi, high_data)
        ):
            opts.append(Operation(code, "buy", "2buy", loss_price, info, "二买"))

        if (
            high_bi.mmd_exists(["2sell"])
            and high_xd_zs.zg < high_bi.high < high_same_bi.high
            and zs_macd_infos.dif_down_cross_num > 0
            and self.bi_td(high_bi, high_data)
        ):
            opts.append(Operation(code, "buy", "2sell", loss_price, info, "二卖"))

        # 线段背驰（中枢震荡）：线段须突破走势中枢 zg/zd，且次笔不创新极值，做反转
        if (
            high_xd.type == "up"
            and high_xd.bc_exists(["xd", "pz", "qs"])
            and high_xd_zs.lines[1].type == "down"
            and high_xd.high >= high_xd_zs.gg
            and high_bi.type == "up"
            and high_bi.index - high_xd.end_line.index == 2
            and price > (high_xd_zs.zg - ((high_xd_zs.zg - high_xd_zs.zd) / 2))
            and high_bi.high < high_xd.end_line.high
            and self.bi_td(high_bi, high_data)
        ):
            opts.append(
                Operation(code, "buy", "up_pz_bc_sell", loss_price, info, "线段背驰")
            )
        if (
            high_xd.type == "down"
            and high_xd.bc_exists(["xd", "pz", "qs"])
            and high_xd_zs.lines[1].type == "up"
            and high_xd.low <= high_xd_zs.dd
            and high_bi.type == "down"
            and high_bi.index - high_xd.end_line.index == 2
            and price < (high_xd_zs.zd + ((high_xd_zs.zg - high_xd_zs.zd) / 2))
            and high_bi.low > high_xd.end_line.low
            and self.bi_td(high_bi, high_data)
        ):
            opts.append(
                Operation(code, "buy", "down_pz_bc_buy", loss_price, info, "线段背驰")
            )

        # 三类买卖点（右侧）：须在走势中枢 zd/zg 一半以上位置，此处使用宽松的 zd/zg 边界
        if (
            high_bi.mmd_exists(["3buy"])
            and high_bi.low > (high_xd_zs.zd + (high_xd_zs.zg - high_xd_zs.zd) / 2)
            and high_xd_zs.lines[-1].index == high_xd.index
            and high_bi_zs.zf() > 30
            and len(high_xd_bi_zss) <= 1
            and high_bi.get_ld(high_data)["macd"]["dif"]["max"] > 0
            and self.bi_td(high_bi, high_data)
        ):
            opts.append(Operation(code, "buy", "3buy", loss_price, info, "三买"))
        if (
            high_bi.mmd_exists(["3sell"])
            and high_bi.high < (high_xd_zs.zg - (high_xd_zs.zg - high_xd_zs.zd) / 2)
            and high_xd_zs.lines[-1].index == high_xd.index
            and high_bi_zs.zf() > 30
            and len(high_xd_bi_zss) <= 1
            and high_bi.get_ld(high_data)["macd"]["dif"]["min"] < 0
            and self.bi_td(high_bi, high_data)
        ):
            opts.append(Operation(code, "buy", "3sell", loss_price, info, "三卖"))

        # 类三买卖点：第一笔入中枢为三买，之后形成新笔中枢且位置更高（卖点反之），是三买的延伸确认
        if (
            high_bi.type == "down"
            and high_bi_zs_start_bi.mmd_exists(["3buy"])
            and len(high_xd_bi_zss) == 2
            and high_xd_zs.lines[-1].index == high_xd.index
            and high_bi_zs.zf() > 30
            and high_bi.low > high_xd_zs.zd
            and high_bi.low > high_bi_zs_start_bi.low
            and high_bi.index == high_bi_zs.lines[-1].index
            and high_bi.get_ld(high_data)["macd"]["dif"]["max"] > 0
            and self.bi_td(high_bi, high_data)
        ):
            opts.append(Operation(code, "buy", "l3buy", loss_price, info, "类三买"))
        if (
            high_bi.type == "up"
            and high_bi_zs_start_bi.mmd_exists(["3sell"])
            and len(high_xd_bi_zss) == 2
            and high_xd_zs.lines[-1].index == high_xd.index
            and high_bi_zs.zf() > 30
            and high_bi.high < high_xd_zs.zg
            and high_bi.high < high_bi_zs_start_bi.high
            and high_bi.index == high_bi_zs.lines[-1].index
            and high_bi.get_ld(high_data)["macd"]["dif"]["max"] < 0
            and self.bi_td(high_bi, high_data)
        ):
            opts.append(Operation(code, "buy", "l3sell", loss_price, info, "类三卖"))

        return opts

    def close(
        self, code, mmd: str, pos: POSITION, market_data: MarketDatas
    ) -> Union[Operation, None]:
        if pos.balance == 0:
            return False

        high_data = market_data.get_cl_data(code, market_data.frequencys[0])

        if (
            len(high_data.get_xds()) == 0
            or len(high_data.get_xd_zss()) == 0
            or len(high_data.get_bi_zss()) == 0
            or len(high_data.get_bis()) == 0
        ):
            return False

        loss_opt = self.check_loss(mmd, pos, high_data.get_klines()[-1].c)
        if loss_opt is not None:
            return loss_opt

        pos_high_bi: BI = pos.info["high_bi"]

        high_xd_zs = high_data.get_xd_zss()[-1]

        high_xd = high_data.get_xds()[-1]
        high_bi = self.last_done_bi(high_data.get_bis())

        # 确保平仓判断使用的是开仓后产生的新笔，避免用开仓笔本身触发平仓
        if high_bi.start.index <= pos_high_bi.start.index:
            return False

        # 在线段中枢上方出现一/二类卖点（买入方向），视为走势反转平仓
        if (
            "buy" in mmd
            and high_bi.mmd_exists(["1sell", "2sell"])
            and high_bi.high > high_xd_zs.zg
            and self.bi_td(high_bi, high_data)
        ):
            return Operation(
                code, "sell", mmd, msg="线段中枢上方出现卖点 %s" % (high_bi.line_mmds())
            )
        if (
            "sell" in mmd
            and high_bi.mmd_exists(["1buy", "2buy"])
            and high_bi.low < high_xd_zs.zd
            and self.bi_td(high_bi, high_data)
        ):
            return Operation(
                code, "sell", mmd, msg="线段中枢下方出现买点 %s" % (high_bi.line_mmds())
            )

        # 三类/类三类买点持仓后，笔出现盘整/趋势背驰且已突破中枢 zg，平仓
        if (
            "3buy" in mmd
            and high_bi.type == "up"
            and high_bi.bc_exists(["pz", "qs"])
            and high_bi.high > high_xd_zs.zg
            and self.bi_td(high_bi, high_data)
        ):
            return Operation(
                code, "sell", mmd, msg="3买后出现笔背驰 %s" % (high_bi.line_bcs())
            )
        if (
            "3sell" in mmd
            and high_bi.type == "down"
            and high_bi.bc_exists(["pz", "qs"])
            and high_bi.low < high_xd_zs.zd
            and self.bi_td(high_bi, high_data)
        ):
            return Operation(
                code, "sell", mmd, msg="3卖后出现笔背驰 %s" % (high_bi.line_bcs())
            )

        # 线段出现三卖点，且次笔不创新高（不破新低），确认走势终结，平仓
        if (
            "buy" in mmd
            and high_xd.mmd_exists(["3sell"])
            and high_bi.type == "up"
            and high_bi.index - high_xd.end_line.index == 2
            and high_bi.high < high_xd.end_line.high
            and self.bi_td(high_bi, high_data)
        ):
            return Operation(code, "sell", mmd, msg="线段出现三卖，并且次笔不创新高")
        if (
            "sell" in mmd
            and high_xd.mmd_exists(["3buy"])
            and high_bi.type == "down"
            and high_bi.index - high_xd.end_line.index == 2
            and high_bi.low > high_xd.end_line.low
            and self.bi_td(high_bi, high_data)
        ):
            return Operation(code, "sell", mmd, msg="线段出现三买，并且次笔不创新低")

        # 线段向上背驰且次笔不创新高，确认线段级别走势结束，平仓
        if (
            "buy" in mmd
            and high_xd.type == "up"
            and high_xd.bc_exists(["xd", "pz", "qs"])
            and high_bi.type == "up"
            and high_bi.index - high_xd.end_line.index == 2
            and high_bi.high < high_xd.end_line.high
            and self.bi_td(high_bi, high_data)
        ):
            return Operation(code, "sell", mmd, msg="线段向上背驰，笔不创新高")
        if (
            "sell" in mmd
            and high_xd.type == "down"
            and high_xd.bc_exists(["xd", "pz", "qs"])
            and high_bi.type == "down"
            and high_bi.index - high_xd.end_line.index == 2
            and high_bi.low > high_xd.end_line.low
            and self.bi_td(high_bi, high_data)
        ):
            return Operation(code, "sell", mmd, msg="线段向下背驰，笔不创新低")

        # 线段背驰买入后，价格超过中枢 zg/zd 时，笔出现背驰即止盈离场
        if (
            "bc_buy" in mmd
            and high_bi.type == "up"
            and high_bi.bc_exists(["bi", "pz", "qs"])
            and high_bi.high >= high_xd_zs.zg
            and self.bi_td(high_bi, high_data)
        ):
            return Operation(
                code,
                "sell",
                mmd,
                msg="价格高于 zg，并且笔背驰 %s" % (high_bi.line_bcs()),
            )
        if (
            "bc_sell" in mmd
            and high_bi.type == "down"
            and high_bi.bc_exists(["bi", "pz", "qs"])
            and high_bi.low <= high_xd_zs.zd
            and self.bi_td(high_bi, high_data)
        ):
            return Operation(
                code,
                "sell",
                mmd,
                msg="价格低于 zd，并且笔背驰 %s" % (high_bi.line_bcs()),
            )

        # 小转大：笔角度 >= 45 度且内部 MACD 背驰（dif 与 hist 方向相反），笔停顿即平仓
        if (
            "buy" in mmd
            and high_bi.type == "up"
            and abs(high_bi.jiaodu()) >= 45
            and self.bi_td(high_bi, high_data)
        ):
            macd_list = high_data.get_idx()["macd"]["hist"][
                high_bi.start.k.k_index : high_bi.end.k.k_index + 1
            ]
            dif_list = high_data.get_idx()["macd"]["dif"][
                high_bi.start.k.k_index : high_bi.end.k.k_index + 1
            ]
            macd_jd = self.points_jiaodu(macd_list, "up")
            dif_jd = self.points_jiaodu(dif_list, "up")
            if dif_jd > 0 and macd_jd < 0:
                return Operation(
                    code,
                    "sell",
                    mmd,
                    msg="笔快速上涨，内部macd背驰 macd jd %s dif jd %s"
                    % (macd_jd, dif_jd),
                )
        if (
            "sell" in mmd
            and high_bi.type == "down"
            and abs(high_bi.jiaodu()) >= 45
            and self.bi_td(high_bi, high_data)
        ):
            macd_list = high_data.get_idx()["macd"]["hist"][
                high_bi.start.k.k_index : high_bi.end.k.k_index + 1
            ]
            dif_list = high_data.get_idx()["macd"]["dif"][
                high_bi.start.k.k_index : high_bi.end.k.k_index + 1
            ]
            macd_jd = self.points_jiaodu(macd_list, "down")
            dif_jd = self.points_jiaodu(dif_list, "down")
            if dif_jd < 0 and macd_jd > 0:
                return Operation(
                    code,
                    "sell",
                    mmd,
                    msg="笔快速上涨，内部macd背驰 macd jd %s dif jd %s"
                    % (macd_jd, dif_jd),
                )

        # 小转大：笔角度 >= 45 度且出现验证分型，笔停顿即平仓
        if (
            "buy" in mmd
            and high_bi.type == "up"
            and abs(high_bi.jiaodu()) >= 45
            and self.bi_yanzhen_fx(high_bi, high_data)
            and self.bi_td(high_bi, high_data)
        ):
            return Operation(code, "sell", mmd, msg="小转大验证分型")
        if (
            "sell" in mmd
            and high_bi.type == "down"
            and abs(high_bi.jiaodu()) >= 45
            and self.bi_yanzhen_fx(high_bi, high_data)
            and self.bi_td(high_bi, high_data)
        ):
            return Operation(code, "sell", mmd, msg="小转大验证分型")

        return False


if __name__ == "__main__":
    from chanlun.backtesting import backtest
    from chanlun.cl_utils import query_cl_chart_config

    cl_config = query_cl_chart_config("a", "SH.000001")
    bt_config = {
        "save_file": str(get_data_path() / "backtest" / "a_d_mmd_single_v0_signal.pkl"),
        "strategy": StrategyASingleAllMmd(),
        "mode": "signal",
        "market": "a",
        "base_code": "SH.000001",
        "codes": ["SH.601009"],
        "frequencys": ["d"],
        "start_datetime": "2018-01-01 00:00:00",
        "end_datetime": "2022-04-20 00:00:00",
        "init_balance": 1000000,
        "fee_rate": 0.0006,
        "max_pos": 8,
        "cl_config": cl_config,
    }

    BT = backtest.BackTest(bt_config)

    BT.run()
    BT.save()
    BT.result()
    print("Done")
