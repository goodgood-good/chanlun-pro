from typing import Dict, List, Union
from chanlun.backtesting.base import *
from chanlun.core.cl_interface import *


class StrategyXDMMD(Strategy):
    """
    沪深线段买卖点
    """

    def __init__(self):
        super().__init__()

        self._max_loss_rate = 10

    def open(
        self, code, market_data: MarketDatas, poss: Dict[str, POSITION]
    ) -> List[Operation]:
        opts = []

        data = market_data.get_cl_data(code, market_data.frequencys[0])
        if (
            len(data.get_xds()) == 0
            or len(data.get_bis()) == 0
            or len(data.get_xd_zss()) == 0
        ):
            return opts

        xd = data.get_xds()[-1]

        if len(xd.line_mmds()) == 0:
            return opts

        # 取最后完成笔及其前一同向笔，用于判断线段转折
        bi_1 = data.get_bis()[-1]
        if bi_1.is_done() is False:
            bi_1 = data.get_bis()[-2]
        bi_2 = data.get_bis()[bi_1.index - 2]

        if (
            xd.type == "up"
            and bi_1.type == "up"
            and bi_1.high < bi_2.high
            and self.bi_td(bi_1, data)
        ):
            # 向上线段：次笔高点低于前同向笔，说明线段可能终结
            pass
        elif (
            xd.type == "down"
            and bi_1.type == "down"
            and bi_1.low > bi_2.low
            and self.bi_td(bi_1, data)
        ):
            # 向下线段：次笔低点高于前同向笔，说明线段可能终结
            pass
        else:
            return opts

        mmds = xd.line_mmds()
        price = data.get_klines()[-1].c

        for mmd in mmds:

            if self._max_loss_rate is not None:
                if "buy" in mmd:
                    loss_price = price - (price * (abs(self._max_loss_rate) / 100))
                    loss_price = max(loss_price, xd.low)
                else:
                    loss_price = price + (price * (abs(self._max_loss_rate) / 100))
                    loss_price = min(loss_price, xd.high)
            else:
                if "buy" in mmd:
                    loss_price = xd.low
                else:
                    loss_price = xd.high

            opts.append(
                Operation(
                    code,
                    "buy",
                    mmd,
                    loss_price,
                    info={
                        "fx_datetime": xd.end.k.date,
                        "cl_datas": {
                            "xd": xd,
                            "price": price,
                        },
                    },
                    msg="%s 级别 (MMD: %s Loss: %s "
                    % (data.get_frequency(), xd.line_mmds(), loss_price),
                )
            )

        return opts

    def close(
        self, code, mmd: str, pos: POSITION, market_data: MarketDatas
    ) -> Union[Dict, None]:
        if pos.balance == 0:
            return False

        data = market_data.get_cl_data(code, market_data.frequencys[0])
        price = data.get_klines()[-1].c

        if pos.loss_price is not None:
            if "buy" in mmd:
                if price < pos.loss_price:
                    return Operation(code, "sell", mmd, msg="%s 止损" % mmd)
            elif "sell" in mmd:
                if price > pos.loss_price:
                    return Operation(code, "sell", mmd, msg="%s 止损" % mmd)

        # 找开仓后第一个反向线段作为平仓判断依据
        pos_xd: XD
        pos_xd = pos.info["cl_datas"]["xd"]
        xd = None
        for _xd in data.get_xds()[::-1]:
            if _xd.start.k.date > pos_xd.start.k.date and _xd.type != pos_xd.type:
                xd = _xd
                break
        if xd is None:
            return False

        bi_1 = data.get_bis()[-1]
        if bi_1.is_done() is False:
            bi_1 = data.get_bis()[-2]
        bi_2 = data.get_bis()[bi_1.index - 2]

        if "buy" in mmd:
            # 做多持仓：笔出现一/二卖点，平仓
            if (
                bi_1.type == "up"
                and self.bi_td(bi_1, data)
                and bi_1.mmd_exists(["1sell", "2sell"])
            ):
                return Operation(
                    code,
                    "sell",
                    mmd,
                    msg="%s %s 笔出现 卖点 (%s) 背驰 （%s），多仓清仓"
                    % (mmd, data.get_frequency(), bi_1.line_mmds(), bi_1.line_bcs()),
                )
            # 次笔不创新高，向上线段可能终结，平仓
            if (
                xd.type == "up"
                and bi_1.type == "up"
                and bi_1.high < bi_2.high
                and self.bi_td(bi_1, data)
            ):
                return Operation(
                    code,
                    "sell",
                    mmd,
                    msg="%s %s 向上线段有可能终结 后笔（%s）低于前笔（%s），多仓清仓"
                    % (mmd, data.get_frequency(), bi_1.high, bi_2.high),
                )
            # 线段出现背驰或卖点，平仓
            if (
                xd.type == "up"
                and xd.is_done()
                and (
                    xd.mmd_exists(["1sell", "2sell", "l2sell", "3sell", "l3sell"])
                    or xd.bc_exists(["xd", "pz", "qs"])
                )
            ):
                return Operation(
                    code,
                    "sell",
                    mmd,
                    msg="%s %s 线段出现 卖点 (%s) 背驰 （%s），多仓清仓"
                    % (mmd, data.get_frequency(), xd.line_mmds(), xd.line_bcs()),
                )

        if "sell" in mmd:
            # 做空持仓：笔出现一/二买点，平仓
            if (
                bi_1.type == "down"
                and self.bi_td(bi_1, data)
                and bi_1.mmd_exists(["1buy", "2buy"])
            ):
                return Operation(
                    code,
                    "sell",
                    mmd,
                    msg="%s %s 笔出现 卖点 (%s) 背驰 （%s），空仓清仓"
                    % (mmd, data.get_frequency(), bi_1.line_mmds(), bi_1.line_bcs()),
                )
            # 次笔不创新低，向下线段可能终结，平仓
            if (
                xd.type == "down"
                and bi_1.type == "down"
                and bi_1.low > bi_2.low
                and self.bi_td(bi_1, data)
            ):
                return Operation(
                    code,
                    "sell",
                    mmd,
                    msg="%s %s 向下线段有可能终结 后笔（%s）高于前笔（%s），空仓清仓"
                    % (mmd, data.get_frequency(), bi_1.low, bi_2.low),
                )
            # 线段出现背驰或买点，平仓
            if (
                xd.type == "down"
                and xd.is_done()
                and (
                    xd.mmd_exists(["1buy", "2buy", "l2buy", "3buy", "l3buy"])
                    or xd.bc_exists(["xd", "pz", "qs"])
                )
            ):
                return Operation(
                    code,
                    "sell",
                    mmd,
                    msg="%s %s 线段出现 买点 (%s) 背驰 （%s），空仓清仓"
                    % (mmd, data.get_frequency(), xd.line_mmds(), xd.line_bcs()),
                )
        return False
