from typing import Dict, List, Union

from chanlun.trading.base import POSITION, MarketDatas, Operation, Strategy
from chanlun.core.types import BI


class StrategyDemo(Strategy):
    """
    策略Demo
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

        data_now = market_data.get_cl_data(code, market_data.frequencys[0])
        # 没有笔或中枢，退出
        if len(data_now.get_bis()) == 0 or len(data_now.get_bi_zss()) == 0:
            return opts

        bi_now = data_now.get_bis()[-1]
        if bi_now.is_done() is False:
            bi_now = data_now.get_bis()[-2]

        if len(bi_now.line_mmds()) == 0:
            return opts

        if self.bi_td(bi_now, data_now) is False:
            return opts

        mmds = bi_now.line_mmds()
        price = data_now.get_klines()[-1].c
        # 止损点取笔结束分型倒数第二根缠论K线的高/低点，比用极值更保守
        ck_kline_2_low = bi_now.end.klines[-2].l
        ck_kline_2_high = bi_now.end.klines[-2].h

        for mmd in mmds:
            if "buy" in mmd and price < ck_kline_2_high:
                continue
            if "sell" in mmd and price > ck_kline_2_low:
                continue

            if self._max_loss_rate is not None:
                if "buy" in mmd:
                    loss_price = price - (price * (abs(self._max_loss_rate) / 100))
                    loss_price = max(loss_price, ck_kline_2_low)
                else:
                    loss_price = price + (price * (abs(self._max_loss_rate) / 100))
                    loss_price = min(loss_price, ck_kline_2_high)
            else:
                if "buy" in mmd:
                    loss_price = ck_kline_2_low
                else:
                    loss_price = ck_kline_2_high

            opts.append(
                Operation(
                    code=code,
                    opt="buy",
                    mmd=mmd,
                    loss_price=loss_price,
                    msg="当前级别 (MMD: %s Loss: %s) "
                    % (bi_now.line_mmds(), loss_price),
                    info={
                        "fx_datetime": bi_now.end.k.date,
                        "cl_datas": {
                            "bi_now": bi_now,
                            "price": price,
                        },
                    },
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

        data_now = market_data.get_cl_data(code, market_data.frequencys[0])
        price = data_now.get_klines()[-1].c

        if "buy" in mmd:
            if price < pos.loss_price:
                return Operation(code, "sell", mmd, msg="%s 止损" % mmd)
        elif "sell" in mmd:
            if price > pos.loss_price:
                return Operation(code, "sell", mmd, msg="%s 止损" % mmd)

        # 找到开仓后第一根完成的反向笔作为平仓判断依据
        pos_bi_now: BI
        pos_bi_now = pos.info["cl_datas"]["bi_now"]
        next_bi_now = None
        for _bi in data_now.get_bis()[::-1]:
            if (
                _bi.start.k.date > pos_bi_now.start.k.date
                and _bi.type != pos_bi_now.type
                and _bi.is_done()
            ):
                next_bi_now = _bi
                break
        if next_bi_now is None:
            return None

        if "buy" in mmd:
            # 做多持仓：反向笔向上停顿，且出现卖点或背驰，平仓
            if (
                next_bi_now.type == "up"
                and self.bi_td(next_bi_now, data_now)
                and (
                    next_bi_now.mmd_exists(
                        ["1sell", "2sell", "l2sell", "3sell", "l3sell"]
                    )
                    or next_bi_now.bc_exists(["bi", "pz", "qs"])
                )
            ):
                return Operation(
                    code=code,
                    opt="sell",
                    mmd=mmd,
                    msg="%s 当前级别出现卖点（MMDS: %s，BC：（BI: %s, PZ: %s, QS: %s）），多仓清仓"
                    % (
                        mmd,
                        next_bi_now.line_mmds(),
                        next_bi_now.bc_exists(["bi"]),
                        next_bi_now.bc_exists(["pz"]),
                        next_bi_now.bc_exists(["qs"]),
                    ),
                )

        if "sell" in mmd:
            # 做空持仓：反向笔向下停顿，且出现买点或背驰，平仓
            if (
                next_bi_now.type == "down"
                and self.bi_td(next_bi_now, data_now)
                and (
                    next_bi_now.mmd_exists(["1buy", "2buy", "l2buy", "3buy", "l3buy"])
                    or next_bi_now.bc_exists(["bi", "pz", "qs"])
                )
            ):
                return Operation(
                    code=code,
                    opt="sell",
                    mmd=mmd,
                    msg="%s 当前级别出现买点（MMDS: %s，BC：（BI: %s, PZ: %s, QS: %s）），空仓清仓"
                    % (
                        mmd,
                        next_bi_now.line_mmds(),
                        next_bi_now.bc_exists(["bi"]),
                        next_bi_now.bc_exists(["pz"]),
                        next_bi_now.bc_exists(["qs"]),
                    ),
                )
        return None


if __name__ == "__main__":

    from chanlun.backtesting import backtest
    from chanlun.cl_utils import query_cl_chart_config
    from chanlun.config import get_data_path
    from chanlun.exchange.exchange_tdx import ExchangeTDX

    ex = ExchangeTDX()
    stocks = ex.all_stocks()
    run_codes = [
        _s["code"] for _s in stocks if _s["code"][0:5] in ["SH.60", "SZ.00", "SZ.30"]
    ]
    run_codes = run_codes[0:100]
    print(f"回测代码数量：{len(run_codes)}")

    cl_config = query_cl_chart_config("a", "SHSE.000001")
    bt_config = {
        "save_file": str(get_data_path() / "backtest" / "a_demo_v0_signal.pkl"),
        "strategy": StrategyDemo(),
        "mode": "signal",
        "market": "a",
        "base_code": "SH.000001",
        "codes": run_codes,
        "frequencys": ["d"],
        "start_datetime": "2020-01-01 00:00:00",
        "end_datetime": "2025-06-01 00:00:00",
        "init_balance": 1000000,
        "fee_rate": 0.001,
        "max_pos": 8,
        "cl_config": cl_config,
    }

    BT = backtest.BackTest(bt_config)

    # 多标的信号模式使用多进程，充分利用多核
    BT.run_process(max_workers=5)
    BT.save()
    BT.result()
    print("Done")
