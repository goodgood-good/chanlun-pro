from typing import Dict, List, Union

from chanlun.backtesting.base import POSITION, MarketDatas, Operation, Strategy


class StrategySonLevel1MMD(Strategy):
    """
    市场：任意
    周期：多周期

    在高级别出现买卖点或背驰，在设置的低级别中有出现过一二类买卖点，则进行开仓
    平仓反过来即可
    """

    def __init__(self):
        super().__init__()

        self._max_loss_rate = 10  # 最大亏损比例设置

    @staticmethod
    def info_msg(infos: dict):
        """
        输出可理解的低级别info信息
        """
        if infos is None:
            return ""
        msg_maps = {
            "qiang_ding_fx": "强顶分型",
            "qiang_di_fx": "强底分型",
            "up_bi_bc": "上笔背",
            "up_xd_bc": "上线背",
            "up_pz_bc": "上盘背",
            "up_qs_bc": "上趋势背",
            "down_bi_bc": "下笔背",
            "down_xd_bc": "下线背",
            "down_pz_bc": "下盘背",
            "down_qs_bc": "下趋势背",
            "1buy": "一买",
            "2buy": "二买",
            "3buy": "三买",
            "l3buy": "类三买",
            "1sell": "一卖",
            "2sell": "二卖",
            "3sell": "三卖",
            "l3sell": "类三卖",
        }
        msg = ""
        for k, v in infos.items():
            if v > 0:
                msg += f"{msg_maps[k]}:{v}"
        return msg

    def open(
        self, code, market_data: MarketDatas, poss: Dict[str, POSITION]
    ) -> List[Operation]:
        """
        开仓监控，返回开仓配置
        """
        opts = []

        high_data = market_data.get_cl_data(code, market_data.frequencys[0])
        # 没有笔或中枢，退出
        if len(high_data.get_bis()) == 0 or len(high_data.get_bi_zss()) == 0:
            return opts
        high_bi = self.last_done_bi(high_data.get_bis())
        # 当前缠论K线距笔结束超过3根，信号已过时
        if high_data.get_cl_klines()[-1].index - high_bi.end.klines[-1].index > 3:
            return opts
        if len(high_bi.line_bcs()) == 0 and (high_bi.line_mmds()) == 0:
            return opts
        if self.bi_td(high_bi, high_data) is False:
            return opts

        # 在高级别笔结束分型的时间范围内，检查低级别是否出现一/二类买卖点
        low_level_1mmd = False
        low_frequency = None
        # 注意：数字货币 K 线前对齐，沪深/期货后对齐，取分型时间范围的方式不同
        if market_data.market == "currency":
            # 前对齐：分型首根 K 线到下一根 K 线起点
            start_cl_k_date = high_bi.end.klines[0].date
            end_cl_k_date = high_data.get_klines()[
                high_bi.end.klines[-1].k_index + 1
            ].date
        else:
            # 后对齐：分型首根 K 线的前一根到分型末根 K 线
            start_cl_k_date = high_data.get_klines()[
                high_bi.end.klines[0].k_index - 1
            ].date
            end_cl_k_date = high_bi.end.klines[-1].date
        low_infos = None
        for f in market_data.frequencys[1:]:
            low_data = market_data.get_cl_data(code, f)
            low_infos = self.check_low_info_by_datetime(
                low_data, start_cl_k_date, end_cl_k_date
            )
            if high_bi.type == "up" and (
                low_infos["1sell"] > 0 or low_infos["2sell"] > 0
            ):
                low_level_1mmd = True
                low_frequency = f
                break
            elif high_bi.type == "down" and (
                low_infos["1buy"] > 0 or low_infos["2buy"] > 0
            ):
                low_level_1mmd = True
                low_frequency = f
                break

        if low_level_1mmd is False:
            return opts

        # 止损取笔分型极值与按最大止损比例的价格中较优的一个
        price = high_data.get_klines()[-1].c
        if self._max_loss_rate is not None:
            if high_bi.type == "up":
                loss_price = min(
                    high_bi.end.klines[-1].h, price * (1 + self._max_loss_rate / 100)
                )
            else:
                loss_price = max(
                    high_bi.end.klines[-1].l, price * (1 - self._max_loss_rate / 100)
                )
        else:
            loss_price = (
                high_bi.end.klines[-1].l
                if high_bi.type == "down"
                else high_bi.end.klines[-1].h
            )

        for mmd in high_bi.line_mmds():
            opts.append(
                Operation(
                    code,
                    "buy",
                    mmd,
                    loss_price,
                    {},
                    f"高级别买卖点 {mmd}, 低级别 {low_frequency} 出现 {self.info_msg(low_infos)}",
                )
            )
        # 仅对盘整/趋势背驰开仓，笔级别背驰（bi）信号太弱不单独开仓
        for bc in high_bi.line_bcs():
            if bc not in ["pz", "qs"]:
                continue
            bc_mmd = f"{high_bi.type}_{bc}_bc_" + (
                "buy" if high_bi.type == "down" else "sell"
            )
            opts.append(
                Operation(
                    code,
                    "buy",
                    bc_mmd,
                    loss_price,
                    {},
                    f"高级别背驰 {bc}，低级别 {low_frequency} 出现 {self.info_msg(low_infos)}",
                )
            )

        return opts

    def close(
        self, code, mmd: str, pos: POSITION, market_data: MarketDatas
    ) -> Union[Operation, None]:
        """
        持仓监控，返回平仓配置
        """
        if pos.balance == 0:
            return None

        high_data = market_data.get_cl_data(code, market_data.frequencys[0])
        if (
            len(high_data.get_bi_zss()) == 0
            or len(high_data.get_bis()) == 0
            or len(high_data.get_xds()) == 0
        ):
            return False

        # 止损判断
        loss_opt = self.check_loss(mmd, pos, high_data.get_klines()[-1].c)
        if loss_opt is not None:
            return loss_opt

        high_bi = self.last_done_bi(high_data.get_bis())
        high_xd = high_data.get_xds()[-1]
        if len(high_bi.line_bcs()) == 0 and len(high_bi.line_mmds()) == 0:
            return False
        if self.bi_td(high_bi, high_data) is False:
            return False

        # 同开仓逻辑，在高级别笔结束分型时间范围内检查低级别一/二类买卖点
        low_level_1mmd = False
        low_frequency = None
        # 注意：数字货币前对齐，沪深/期货后对齐，取时间范围方式不同
        if market_data.market == "currency":
            start_cl_k_date = high_bi.end.klines[0].date
            end_cl_k_date = high_data.get_klines()[
                high_bi.end.klines[-1].k_index + 1
            ].date
        else:
            start_cl_k_date = high_data.get_klines()[
                high_bi.end.klines[0].k_index - 1
            ].date
            end_cl_k_date = high_bi.end.klines[-1].date

        low_infos = None
        for f in market_data.frequencys[1:]:
            low_data = market_data.get_cl_data(code, f)
            low_infos = self.check_low_info_by_datetime(
                low_data, start_cl_k_date, end_cl_k_date
            )
            if high_bi.type == "up" and (low_infos["1sell"] > 0 or low_infos["2sell"]):
                low_level_1mmd = True
                low_frequency = f
                break
            elif high_bi.type == "down" and (
                low_infos["1buy"] > 0 or low_infos["2buy"]
            ):
                low_level_1mmd = True
                low_frequency = f
                break

        if low_level_1mmd is False:
            return False

        if (
            "buy" in mmd
            and high_bi.type == "up"
            and (
                high_bi.mmd_exists(["1sell", "2sell", "3sell", "l3sell"])
                or high_bi.bc_exists(["pz", "qs"])
            )
        ):
            return Operation(
                code,
                "sell",
                mmd,
                msg=f"高级笔（{high_bi.line_mmds()} / {high_bi.line_bcs()}），低级别 {low_frequency} 出现 {self.info_msg(low_infos)}",
            )
        elif (
            "sell" in mmd
            and high_bi.type == "down"
            and (
                high_bi.mmd_exists(["1buy", "2buy", "3buy", "l3buy"])
                or high_bi.bc_exists(["pz", "qs"])
            )
        ):
            return Operation(
                code,
                "sell",
                mmd,
                msg=f"高级笔（{high_bi.line_mmds()} / {high_bi.line_bcs()}），低级别 {low_frequency} 出现 {self.info_msg(low_infos)}",
            )
        elif (
            "buy" in mmd
            and high_xd.type == "up"
            and high_xd.type == high_bi.type
            and high_xd.end_line.index == high_bi.index
            and (
                high_xd.mmd_exists(["1sell", "2sell", "3sell", "l3sell"])
                or high_xd.bc_exists(["xd", "pz", "qs"])
            )
        ):
            return Operation(
                code,
                "sell",
                mmd,
                msg=f"高级线段（{high_xd.line_mmds()} / {high_xd.line_bcs()}），低级别 {low_frequency} 出现 {self.info_msg(low_infos)}",
            )
        elif (
            "sell" in mmd
            and high_xd.type == "down"
            and high_xd.type == high_bi.type
            and high_xd.end_line.index == high_bi.index
            and (
                high_xd.mmd_exists(["1buy", "2buy", "3buy", "l3buy"])
                or high_xd.bc_exists(["xd", "pz", "qs"])
            )
        ):
            return Operation(
                code,
                "sell",
                mmd,
                msg=f"高级线段（{high_xd.line_mmds()} / {high_xd.line_bcs()}），低级别 {low_frequency} 出现 {self.info_msg(low_infos)}",
            )

        return None
