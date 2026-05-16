from typing import Dict, List, Union

from chanlun.backtesting.base import POSITION, MarketDatas, Operation, Strategy
from chanlun.backtesting.klines_generator import KlinesGenerator
from chanlun.cl_analyse import MultiLevelAnalyse


class StrategyCustomLevelMLA(Strategy):
    """
    通过低级别 K线，合成高级别K线，并用多级别分析，来判断高级别笔是否完成

    市场：期货
    周期：单周期（内部合成多周期）
    开仓策略：高级别笔出现买卖点，并多级别分析中，低级别出现盘整或趋势背驰
    平仓策略：开仓的反向笔，低级别分析出现盘整或趋势背驰，或者在收盘前进行平仓
    """

    def __init__(self, high_minutes=5):
        super().__init__()

        self.kg = KlinesGenerator(high_minutes, None, dt_align_type="bob")

    def open(
        self, code, market_data: MarketDatas, poss: Dict[str, POSITION]
    ) -> List[Operation]:
        """
        开仓监控，返回开仓配置
        """
        # KlinesGenerator 需要缠论配置，从回测上下文中实时获取而非构造时固定
        self.kg.cl_config = market_data.cl_config

        opts = []
        low_klines = market_data.klines(code, market_data.frequencys[0])
        high_data = self.kg.update_klines(low_klines)
        if len(high_data.get_bis()) == 0:
            return opts

        high_bi = high_data.get_bis()[-1]

        # 当前缠论K线距离笔结束超过4根，说明已进入新的走势，不再入场
        if high_data.get_cl_klines()[-1].index - high_bi.end.k.index > 4:
            return opts

        # 笔须有买卖点或盘整/趋势背驰才入场
        if len(high_bi.line_mmds()) and high_bi.bc_exists(["pz", "qs"]) is False:
            return opts

        # 对应中枢须经历 MACD 零轴回拉，确认走势能量充分释放
        for mmd in high_bi.mmds:
            if mmd.zs is not None and self.judge_macd_back_zero(high_data, mmd.zs) == 0:
                return opts
        for bc in high_bi.bcs:
            if (
                bc.bc
                and bc.type in ["pz", "qs"]
                and self.judge_macd_back_zero(high_data, bc.zs) == 0
            ):
                return opts

        # 低级别出现盘整或趋势背驰，且低级别笔已停顿，双重确认入场
        low_data = market_data.get_cl_data(code, market_data.frequencys[0])
        mla = MultiLevelAnalyse(high_data, low_data)
        low_info = mla.low_level_qs(high_bi, "bi")
        if low_info.pz_bc is False and low_info.qs_bc is False:
            return opts

        if self.bi_td(low_info.last_line, low_data) is False:
            return opts

        # 止损设在高级别笔结束分型的极值（顶/底）
        loss_price = high_bi.end.val

        for mmd in high_bi.line_mmds():
            opts.append(
                Operation(
                    code=code,
                    opt="buy",
                    mmd=mmd,
                    loss_price=loss_price,
                    info={},
                    msg=f"高级别出现买卖点 {mmd} 低级别趋势 PZBC {low_info.pz_bc} QSBC {low_info.qs_bc}",
                )
            )
            return opts
        for bc in high_bi.line_bcs():
            if bc not in ["pz", "qs"]:
                continue
            # 按方向构造背驰 mmd 名，格式：down_pz_bc_buy / up_qs_bc_sell
            mmd = f'{high_bi.type}_{bc}_bc_{("buy" if high_bi.type == "down" else "sell")}'
            opts.append(
                Operation(
                    code=code,
                    opt="buy",
                    mmd=mmd,
                    loss_price=loss_price,
                    info={},
                    msg=f"高级别出现背驰 {bc} 低级别趋势 PZBC {low_info.pz_bc} QSBC {low_info.qs_bc}",
                )
            )
        return opts

    def close(
        self, code, mmd: str, pos: POSITION, market_data: MarketDatas
    ) -> Union[Operation, None]:
        """
        持仓监控，返回平仓配置

        持仓反向笔，低级别出现 盘整或趋势背驰退出
        """
        if pos.balance == 0:
            return None

        low_klines = market_data.klines(code, market_data.frequencys[0])
        high_data = self.kg.update_klines(low_klines)
        low_data = market_data.get_cl_data(code, market_data.frequencys[0])
        if len(high_data.get_bis()) == 0:
            return None

        price = high_data.get_klines()[-1].c
        loss_opt = self.check_loss(mmd, pos, price)
        if loss_opt is not None:
            return loss_opt

        high_bi = high_data.get_bis()[-1]

        # 平仓条件与开仓对称：低级别盘整/趋势背驰且笔停顿
        mla = MultiLevelAnalyse(high_data, low_data)
        low_info = mla.low_level_qs(high_bi, "bi")
        if low_info.pz_bc is False and low_info.qs_bc is False:
            return None

        if self.bi_td(low_info.last_line, low_data) is False:
            return None

        if "buy" in mmd and high_bi.type == "up":
            return Operation(
                code,
                "sell",
                mmd,
                msg=f"高级别向上笔，低级别趋势 PZBC {low_info.pz_bc} QSBC {low_info.qs_bc}",
            )
        if "sell" in mmd and high_bi.type == "down":
            return Operation(
                code,
                "sell",
                mmd,
                msg=f"高级别向上笔，低级别趋势 PZBC {low_info.pz_bc} QSBC {low_info.qs_bc}",
            )

        return None
