from typing import Dict, List, Union

from chanlun.backtesting.base import POSITION, MarketDatas, Operation, Strategy
from chanlun.cl_interface import LINE


class StrategyFuturesXDZS(Strategy):
    """
    中枢操作法

    当前形成线段中枢：
        在中枢下方，向下线段完成，笔不创向下线段底特征序列分型第三特征的低点，买入做多
        在中枢上方，向上线段完成，笔不创向上线段顶特征序列分型第三特征的高点，卖出做空

    卖出条件，见好就收
    """

    def __init__(self):
        super().__init__()

        self._max_loss_rate = 3

    def open(
        self, code, market_data: MarketDatas, poss: Dict[str, POSITION]
    ) -> List[Operation]:
        """开仓判断：未完成线段中枢内，反向线段完成且笔不破特征序列分型极值时进场"""
        opts = []

        high_data = market_data.get_cl_data(code, market_data.frequencys[0])
        if len(high_data.get_xd_zss()) == 0:
            return opts

        price = high_data.get_klines()[-1].c

        high_bi = high_data.get_bis()[-1]
        high_xd = self.last_done_xd(high_data.get_xds())
        high_xd_zs = high_data.get_xd_zss()[-1]

        # 中枢已完成则不再震荡操作
        if high_xd_zs.done is True:
            return opts

        high_xd_zs_mid_price = (high_xd_zs.zg - high_xd_zs.zd) / 2 + high_xd_zs.zd

        def get_loss_price(line: LINE) -> float:
            """按线段方向取止损价，并受 _max_loss_rate 最大亏损比例约束"""
            if self._max_loss_rate is not None:
                if line.type == "up":
                    loss_price = min(line.high, price * (1 + self._max_loss_rate / 100))
                else:
                    loss_price = max(line.low, price * (1 - self._max_loss_rate / 100))
            else:
                loss_price = line.low if line.type == "down" else line.high
            return loss_price

        info = {
            "zs_mid_price": high_xd_zs_mid_price,
            "zs_zf": high_xd_zs.zf(),
            "zs_lines": high_xd_zs.line_num,
        }

        # 价格在中枢上方，并且向上线段完成
        if price > high_xd_zs.zg and high_xd.type == "up" and high_xd.done:
            # 后续向上笔不超过 顶特征序列分型的第三元素高点，做空
            if (
                high_bi.type == "up"
                and high_bi.high < high_xd.ding_fx.xls[-1].max
                and self.bi_td(high_bi, high_data)
            ):
                return [
                    Operation(
                        code,
                        "buy",
                        "1sell",
                        get_loss_price(high_bi),
                        info,
                        "中枢震荡，向上线段完成，卖出做空",
                    )
                ]

        if price < high_xd_zs.zd and high_xd.type == "down" and high_xd.done:
            # 后续向下笔不超过 底特征序列分型的第三元素低点，做多
            if (
                high_bi.type == "down"
                and high_bi.low > high_xd.di_fx.xls[-1].min
                and self.bi_td(high_bi, high_data)
            ):
                return [
                    Operation(
                        code,
                        "buy",
                        "1buy",
                        get_loss_price(high_bi),
                        info,
                        "中枢震荡，向下线段完成，买入做多",
                    )
                ]
        return opts

    def close(
        self, code, mmd: str, pos: POSITION, market_data: MarketDatas
    ) -> Union[Operation, None, List[Operation]]:
        """平仓判断：止盈止损、保本，或同向线段完成、超中枢边界后笔背驰/买卖点"""
        if pos.balance == 0:
            return False

        high_data = market_data.get_cl_data(code, market_data.frequencys[0])
        price = high_data.get_klines()[-1].c

        # 止盈止损检查
        loss_opt = self.check_loss(mmd, pos, price)
        if loss_opt is not None:
            return loss_opt

        # 保本操作
        self.break_even(pos, 2)

        opts = []

        if len(high_data.get_xd_zss()) == 0:
            return opts

        high_bi = high_data.get_bis()[-1]
        high_xd = high_data.get_xds()[-1]
        high_xd_zs = high_data.get_xd_zss()[-1]

        # 平仓条件：同向线段完成，或价格超出中枢 zg/zd 后笔出现背驰/买卖点
        if "buy" in mmd and high_xd.type == "up" and high_xd.done:
            opts.append(Operation(code, "sell", mmd, msg="向上线段完成，卖出平仓"))
        if "sell" in mmd and high_xd.type == "down" and high_xd.done:
            opts.append(Operation(code, "sell", mmd, msg="向下线段完成，卖出平仓"))

        if (
            "buy" in mmd
            and price > high_xd_zs.zd
            and high_bi.type == "up"
            and self.bi_td(high_bi, high_data)
            and (
                high_bi.bc_exists(["pz", "qs"], "|")
                or high_bi.mmd_exists(["1sell", "2sell", "3sell"], "|")
            )
        ):
            opts.append(
                Operation(
                    code,
                    "sell",
                    mmd,
                    msg=f'做多超过中枢低点后，笔出现 背驰 （{high_bi.line_bcs("|")}） 买卖点 （{high_bi.line_mmds("|")}） 后平仓',
                )
            )

        if (
            "sell" in mmd
            and price < high_xd_zs.zg
            and high_bi.type == "down"
            and self.bi_td(high_bi, high_data)
            and (
                high_bi.bc_exists(["pz", "qs"], "|")
                or high_bi.mmd_exists(["1buy", "2buy", "3buy"], "|")
            )
        ):
            opts.append(
                Operation(
                    code,
                    "sell",
                    mmd,
                    msg=f'做空跌破中枢高点后，笔出现 背驰 （{high_bi.line_bcs("|")}） 买卖点 （{high_bi.line_mmds("|")}） 后平仓',
                )
            )

        # 如果买入做多后，价格超过中枢 zg （中枢高点），即使一个笔背驰，也平仓出来
        if (
            "buy" in mmd
            and price > high_xd_zs.zg
            and high_bi.type == "up"
            and self.bi_td(high_bi, high_data)
            and high_bi.bc_exists(["bi", "pz", "qs"], "|")
        ):
            opts.append(
                Operation(
                    code,
                    "sell",
                    mmd,
                    msg=f'做多超过中枢高点后，笔出现 背驰 （{high_bi.line_bcs("|")}）后平仓',
                )
            )
        # 如果买入做空后，价格超过中枢 zd （中枢低点），即使一个笔背驰，也平仓出来
        if (
            "sell" in mmd
            and price < high_xd_zs.zd
            and high_bi.type == "down"
            and self.bi_td(high_bi, high_data)
            and high_bi.bc_exists(["bi", "pz", "qs"], "|")
        ):
            opts.append(
                Operation(
                    code,
                    "sell",
                    mmd,
                    msg=f'做空超过中枢低点后，笔出现 背驰 （{high_bi.line_bcs("|")}）后平仓',
                )
            )

        return opts
