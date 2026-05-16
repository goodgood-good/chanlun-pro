from typing import Dict, List, Union

from chanlun.backtesting.base import POSITION, MarketDatas, Operation, Strategy


class StrategyLastZs3mmd(Strategy):
    """
    基于最后一个笔中枢，形成的三类买卖点进行交易
    在根据线段与强分析进行过滤
    """

    def __init__(self):
        super().__init__()

        self._max_loss_rate = None  # 最大亏损比例设置

    def open(
        self, code, market_data: MarketDatas, poss: Dict[str, POSITION]
    ) -> List[Operation]:
        """
        开仓监控，返回开仓配置
        """
        opts = []

        high_data = market_data.get_cl_data(code, market_data.frequencys[0])
        # 没有笔或中枢，退出
        if (
            len(high_data.get_bis()) <= 5
            or len(high_data.get_bi_zss()) < 2
            or len(high_data.get_xds()) == 0
        ):
            return opts

        # 仅对已完成的笔中枢判断三类买卖点，未完成中枢方向未确定
        high_last_bi_zs = high_data.get_last_bi_zs()
        if high_last_bi_zs is None or high_last_bi_zs.done is False:
            return opts

        high_last_done_bi = self.last_done_bi(high_data.get_bis())
        high_last_xd = high_data.get_xds()[-1]
        last_price = high_data.get_klines()[-1].c

        # 振幅过小的中枢缺乏有效震荡，三类买卖点可靠性低
        if high_last_bi_zs.zf() <= 30:
            return opts

        # 最后分型须为强分型（力度 >= 5），且价格已突破分型确认 K 线极值
        high_fx = high_data.get_fxs()[-1]
        if (
            high_fx.type == "di"
            and high_fx.ld() >= 5
            and high_last_bi_zs.lines[1].mmd_exists(["3buy"])
            and high_fx.val > high_last_bi_zs.zd
            and last_price > high_fx.klines[-1].h
        ):
            opts.append(
                Operation(
                    code,
                    "buy",
                    "l3buy",
                    high_fx.val,
                    {"fx_datetime": high_last_done_bi.end.k.date},
                    "三买后强低分型买入",
                )
            )

        if (
            high_fx.type == "ding"
            and high_fx.ld() >= 5
            and high_last_bi_zs.lines[1].mmd_exists(["3sell"])
            and high_fx.val < high_last_bi_zs.zg
            and last_price < high_fx.klines[-1].l
        ):
            opts.append(
                Operation(
                    code,
                    "buy",
                    "l3sell",
                    high_fx.val,
                    {"fx_datetime": high_last_done_bi.end.k.date},
                    "三卖后强顶分型卖出",
                )
            )

        # 止损设在笔结束分型极值
        loss_price = high_last_done_bi.end.val

        # 最后一笔须是当前最新笔，避免中枢被延伸后误判
        if high_last_done_bi.index != high_data.get_bis()[-1].index:
            return opts

        if (
            high_last_done_bi.type == "down"
            and high_last_done_bi.low > high_last_bi_zs.zg
        ):
            # 三类买点：线段须向下完成或向上延伸（走势方向允许反转）
            if (high_last_xd.type == "down" and high_last_xd.is_done()) or (
                high_last_xd.type == "up" and high_last_xd.is_done() is False
            ):
                # 中枢内部须有强底分型（力度 >= 5），说明中枢有有效支撑
                exists_qfx = False
                fxs = high_data.get_fxs()[
                    high_last_bi_zs.lines[1]
                    .start.index : high_last_bi_zs.lines[-2]
                    .end.index
                    + 1
                ]
                for fx in fxs:
                    if fx.type == "di" and fx.ld() >= 5:
                        exists_qfx = True
                        break
                # 中枢内部未出现一/二类卖点，说明中枢方向未被破坏
                exists_sell_mmd = False
                for line in high_last_bi_zs.lines:
                    if line.mmd_exists(["1sell", "2sell"]):
                        exists_sell_mmd = True
                        break
                if (
                    exists_qfx is True
                    and exists_sell_mmd is False
                    and last_price > high_last_done_bi.end.klines[-1].h
                ):
                    opts.append(
                        Operation(
                            code,
                            "buy",
                            "3buy",
                            loss_price,
                            {"fx_datetime": high_last_done_bi.end.k.date},
                            "笔中枢三买",
                        )
                    )

        if (
            high_last_done_bi.type == "up"
            and high_last_done_bi.high < high_last_bi_zs.zd
        ):
            # 三类卖点：线段须向上完成或向下延伸
            if (high_last_xd.type == "up" and high_last_xd.is_done()) or (
                high_last_xd.type == "down" and high_last_xd.is_done() is False
            ):
                # 中枢内部须有强顶分型（力度 >= 5）
                exists_qfx = False
                fxs = high_data.get_fxs()[
                    high_last_bi_zs.lines[1]
                    .start.index : high_last_bi_zs.lines[-2]
                    .end.index
                    + 1
                ]
                for fx in fxs:
                    if fx.type == "ding" and fx.ld() >= 5:
                        exists_qfx = True
                        break
                # 中枢内部未出现一/二类买点，说明中枢方向未被破坏
                exists_buy_mmd = False
                for line in high_last_bi_zs.lines:
                    if line.mmd_exists(["1buy", "2buy"]):
                        exists_buy_mmd = True
                        break
                if (
                    exists_qfx is True
                    and exists_buy_mmd is False
                    and last_price < high_last_done_bi.end.klines[-1].l
                ):
                    opts.append(
                        Operation(
                            code,
                            "buy",
                            "3sell",
                            loss_price,
                            {"fx_datetime": high_last_done_bi.end.k.date},
                            "笔中枢三卖",
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
        price = high_data.get_klines()[-1].c
        loss_opt = self.check_loss(mmd, pos, price)
        if loss_opt:
            return loss_opt

        high_bi = self.last_done_bi(high_data.get_bis())

        # 笔出现卖点（停顿确认），平仓
        if (
            "buy" in mmd
            and high_bi.type == "up"
            and self.bi_td(high_bi, high_data)
            and high_bi.mmd_exists(["1sell", "2sell", "3sell", "l3sell"])
        ):
            return Operation(
                code, "sell", mmd, msg="高级别笔卖点（%s）" % high_bi.line_mmds()
            )
        if (
            "sell" in mmd
            and high_bi.type == "down"
            and self.bi_td(high_bi, high_data)
            and high_bi.bc_exists(["1buy", "2buy", "3buy", "l3buy"])
        ):
            return Operation(
                code, "sell", mmd, msg="高级别笔买点（%s）" % high_bi.line_mmds()
            )

        # 笔出现背驰（停顿确认），平仓
        if (
            "buy" in mmd
            and high_bi.type == "up"
            and self.bi_td(high_bi, high_data)
            and high_bi.bc_exists(["pz", "qs"])
        ):
            return Operation(
                code, "sell", mmd, msg="高级别笔背驰（%s）" % high_bi.line_bcs()
            )
        if (
            "sell" in mmd
            and high_bi.type == "down"
            and self.bi_td(high_bi, high_data)
            and high_bi.bc_exists(["pz", "qs"])
        ):
            return Operation(
                code, "sell", mmd, msg="高级别笔背驰（%s）" % high_bi.line_bcs()
            )

        return None
