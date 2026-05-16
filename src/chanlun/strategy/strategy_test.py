from typing import Dict, List, Union

from chanlun.backtesting.base import POSITION, MarketDatas, Operation, Strategy
from chanlun.config import get_data_path


class StrategyTest(Strategy):
    """
    策略测试类
    """

    def __init__(self):
        super().__init__()

    def open(
        self, code, market_data: MarketDatas, poss: Dict[str, POSITION]
    ) -> List[Operation]:
        """
        开仓监控，返回开仓配置
        """
        opts = []

        klines = market_data.klines(code, market_data.frequencys[0])
        last_k = klines.iloc[-1]
        if last_k["close"] > last_k["open"]:
            # 阳线买入
            opts.append(
                Operation(
                    code,
                    "buy",
                    "1buy",
                    loss_price=last_k["low"],
                    info={},
                    msg="看涨K线",
                    pos_rate=0.5,
                    key=f"{code}:{last_k['date']}",
                )
            )
        elif last_k["close"] < last_k["open"]:
            # 阴线卖出（做空）
            opts.append(
                Operation(
                    code,
                    "buy",
                    "1sell",
                    loss_price=last_k["high"],
                    info={},
                    msg="看跌K线",
                    pos_rate=0.5,
                    key=f"{code}:{last_k['date']}",
                )
            )

        return opts

    def close(
        self, code, mmd: str, pos: POSITION, market_data: MarketDatas
    ) -> Union[Operation, None]:
        """
        持仓监控，返回平仓配置
        """
        opts = []

        klines = market_data.klines(code, market_data.frequencys[0])
        last_k = klines.iloc[-1]
        loss_opt = self.check_loss(mmd, pos, last_k["close"])
        if loss_opt is not None:
            opts.append(loss_opt)

        # 做多持仓：出现阴线时平仓一部分（pos_rate=0.5）
        if "buy" in mmd:
            if last_k["close"] < last_k["open"]:
                opts.append(
                    Operation(
                        code,
                        "sell",
                        mmd,
                        msg="看跌k平仓",
                        pos_rate=0.5,
                        key=f"{code}:{last_k['date']}",
                    )
                )
        # 做空持仓：出现阳线时平仓一部分（pos_rate=0.5）
        if "sell" in mmd:
            if last_k["close"] > last_k["open"]:
                opts.append(
                    Operation(
                        code,
                        "sell",
                        mmd,
                        msg="看涨k平仓",
                        pos_rate=0.5,
                        key=f"{code}:{last_k['date']}",
                    )
                )

        return opts


if __name__ == "__main__":
    from chanlun.backtesting import backtest
    from chanlun.cl_utils import query_cl_chart_config

    market = "futures"
    cl_config = query_cl_chart_config(market, "SH.000001")
    bt_config = {
        "save_file": str(get_data_path() / "backtest" / "strategy_test.pkl"),
        "strategy": StrategyTest(),
        "mode": "signal",
        "market": market,
        "base_code": "SHFE.RB",
        "codes": ["SHFE.RB"],
        "frequencys": ["5m"],
        "start_datetime": "2024-11-01 00:00:00",
        "end_datetime": "2025-11-02 00:00:00",
        "init_balance": 100000,
        "fee_rate": 0.0003,
        "max_pos": 1,
        "cl_config": cl_config,
    }

    BT = backtest.BackTest(bt_config)

    BT.run()
    BT.save()
    BT.result()
    print("Done")
