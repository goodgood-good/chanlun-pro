import datetime
from typing import Union

from chanlun.backtesting.base import POSITION, MarketDatas, Operation, Strategy, Trader
from chanlun.core.cl_interface import Dict, List
from chanlun.config import get_data_path


class StrategyADMMDTest(Strategy):
    """
    沪深A股，日线级别买卖点
    """

    def __init__(
        self, mode="test", filter_key: str = "loss_rate", filter_reverse: bool = True
    ):
        super().__init__()

        self.mode = mode
        self.filter_key: str = filter_key
        self.filter_reverse: bool = filter_reverse

        self.mmds = [
            "1buy",
            "2buy",
            "l2buy",
            "3buy",
            "l3buy",
            "1sell",
            "2sell",
            "l2sell",
            "3sell",
            "l3sell",
        ]
        self.bi_bcs = ["bi", "pz", "qs"]
        self.xd_bcs = ["xd", "pz", "qs"]

        self.zs_code = "SHSE.000001"  # 上证指数的代码

    def clear(self):
        self.tz = None
        self._cache_open_infos = []
        return super().clear()

    def is_filter_opts(self):
        return True

    def filter_opts(self, opts: List[Operation], trader: Trader = None):
        """对开/平仓操作排序：平仓优先执行，买入按指定字段排序（控制持仓优先级）。"""
        if len(opts) == 0:
            return opts
        buy_opts = [_o for _o in opts if _o.opt == "buy"]
        sell_opts = [_o for _o in opts if _o.opt == "sell"]
        # 风险越大（止损率/涨幅越大）的信号优先买入，通过 filter_reverse 可切换排序方向
        buy_opts = sorted(
            opts, key=lambda x: x.info[self.filter_key], reverse=self.filter_reverse
        )

        # 平仓操作先于开仓，避免满仓时无法执行新买入
        return sell_opts + buy_opts

    def open(
        self, code, market_data: MarketDatas, poss: Dict[str, POSITION]
    ) -> List[Operation]:
        opts = []

        cd_d = market_data.get_cl_data(code, market_data.frequencys[1])
        if len(cd_d.get_bis()) == 0:
            return opts
        price = cd_d.get_src_klines()[-1].c
        bi_d = cd_d.get_bis()[-1]
        # 只做向下笔结束后的买点（反转），向上笔延续时不入场
        if bi_d.type == "up":
            return opts
        if len(bi_d.line_mmds("|")) == 0:
            return opts
        if bi_d.is_done() is False:
            return opts

        k_now_d = cd_d.get_src_klines()[-1]
        k_pre_d = cd_d.get_src_klines()[-2]
        # 量价配合过滤：要求当日收盘上涨且成交量放大，确认向上动能
        if k_now_d.c < k_pre_d.c:
            return opts
        if k_now_d.a < k_pre_d.a:
            return opts

        # 涨停日不入场：当日最高触及涨停价，次日可能无法卖出
        zt_price = self.code_zt_price(code, k_pre_d.c)
        if zt_price - 0.01 <= k_now_d.h <= zt_price + 0.01:
            return opts

        # 构建开仓特征 DataFrame，后续可通过 pos_querys 对特征列做 query 过滤
        pos_df = []
        for _mmd in bi_d.line_mmds("|"):
            pos_df.append(
                {
                    "opt_mmd": _mmd,
                    "__open_k_date": cd_d.get_src_klines()[-1].date,
                }
            )
        pos_df = pd.DataFrame(pos_df)

        # 日线笔/线段特征（供后续 query 过滤使用）
        if True:
            bi_pre_d = cd_d.get_bis()[-2]
            xd_d = cd_d.get_xds()[-1]
            pos_df["k_now_d_change"] = (k_now_d.c - k_pre_d.c) / k_pre_d.c * 100
            pos_df["k_now_volume_by_pre"] = k_now_d.a / k_pre_d.a
            pos_df["bi_pre_d_mmds"] = "/".join(sorted(bi_pre_d.line_mmds("|")))
            pos_df["bi_pre_d_bcs"] = "/".join(sorted(bi_pre_d.line_bcs("|")))
            pos_df["xd_d_type"] = f"{xd_d.type}_{xd_d.is_done()}"

        # 日线均线与 MACD 指标特征
        if True:
            idx_ma5 = self.idx_ma(cd_d, 5)
            idx_ma10 = self.idx_ma(cd_d, 10)
            idx_ma20 = self.idx_ma(cd_d, 20)
            pos_df["idx_ma_5_by_price"] = price > idx_ma5[-1]
            pos_df["idx_ma_10_by_price"] = price > idx_ma10[-1]
            pos_df["idx_ma_20_by_price"] = price > idx_ma20[-1]
            pos_df["idx_ma_5_by_ma_10"] = idx_ma5[-1] > idx_ma10[-1]
            pos_df["idx_ma_5_by_ma_20"] = idx_ma5[-1] > idx_ma20[-1]
            pos_df["idx_ma_10_by_ma_20"] = idx_ma10[-1] > idx_ma20[-1]

            idx_macd = cd_d.get_idx()["macd"]
            pos_df["idx_macd_hist_by_0"] = idx_macd["hist"][-1] > 0
            pos_df["idx_macd_dif_by_0"] = idx_macd["dif"][-1] > 0
            pos_df["idx_macd_dea_by_0"] = idx_macd["dea"][-1] > 0

        # 周线笔/线段/均线特征（大级别趋势背景）
        cd_w = market_data.get_cl_data(code, market_data.frequencys[0])
        if len(cd_w.get_xds()) == 0:
            return opts

        if True:
            bi_w = cd_w.get_bis()[-1]
            xd_w = cd_w.get_xds()[-1]
            pos_df["bi_w_type"] = f"{bi_w.type}_{bi_w.is_done()}"
            pos_df["xd_w_type"] = f"{xd_w.type}_{bi_w.is_done()}"

        if True:
            idx_ma5_w = self.idx_ma(cd_w, 5)
            idx_ma10_w = self.idx_ma(cd_w, 10)
            idx_ma20_w = self.idx_ma(cd_w, 20)
            pos_df["idx_ma_5_w_by_price"] = price > idx_ma5_w[-1]
            pos_df["idx_ma_10_w_by_price"] = price > idx_ma10_w[-1]
            pos_df["idx_ma_20_w_by_price"] = price > idx_ma20_w[-1]
            pos_df["idx_ma_5_by_ma_10_w"] = idx_ma5_w[-1] > idx_ma10_w[-1]
            pos_df["idx_ma_5_by_ma_20_w"] = idx_ma5_w[-1] > idx_ma20_w[-1]
            pos_df["idx_ma_10_by_ma_20_w"] = idx_ma10_w[-1] > idx_ma20_w[-1]

        # 上证指数大盘特征（市场环境过滤）
        if True:
            cd_d_zs = market_data.get_cl_data(self.zs_code, market_data.frequencys[1])
            bi_d_zs = cd_d_zs.get_bis()[-1]
            pos_df["zs_bi_type"] = f"{bi_d_zs.type}_{bi_d_zs.is_done()}"
            zs_ma5 = self.idx_ma(cd_d_zs, 5)
            zs_ma10 = self.idx_ma(cd_d_zs, 10)
            zs_ma20 = self.idx_ma(cd_d_zs, 20)
            zs_price = cd_d_zs.get_src_klines()[-1].c
            pos_df["zs_ma_5_by_price"] = zs_price > zs_ma5[-1]
            pos_df["zs_ma_10_by_price"] = zs_price > zs_ma10[-1]
            pos_df["zs_ma_20_by_price"] = zs_price > zs_ma20[-1]
            pos_df["zs_ma_5_by_ma_10"] = zs_ma5[-1] > zs_ma10[-1]
            pos_df["zs_ma_5_by_ma_20"] = zs_ma5[-1] > zs_ma20[-1]
            pos_df["zs_ma_10_by_ma_20"] = zs_ma10[-1] > zs_ma20[-1]

        # 止损价使用当日最低点，loss_rate 为止损比例（供 filter_opts 排序使用）
        if True:
            pos_df["__loss_price"] = k_now_d.l
            pos_df["loss_rate"] = (price - k_now_d.l) / price * 100

        # pos_querys 为空时不过滤，可在此追加 pandas query 字符串实现特征筛选
        pos_querys = []
        for _q in pos_querys:
            pos_df = pos_df.query(_q)

        if len(pos_df) == 0:
            return opts

        for _, _pos in pos_df.iterrows():
            opts.append(
                Operation(
                    code=code,
                    opt="buy",
                    mmd=_pos["opt_mmd"],
                    loss_price=_pos["__loss_price"],
                    info=_pos.to_dict(),
                    msg=f"买点 {_pos['opt_mmd']} , 止损价格 {_pos['__loss_price']}",
                    open_uid=f"{code}_{bi_d.start.k.date}_{_pos['opt_mmd']}",
                )
            )

        return opts

    def close(
        self, code, mmd: str, pos: POSITION, market_data: MarketDatas
    ) -> Union[Operation, None, List[Operation]]:
        """
        平仓操作信号
        """
        opts = []
        if pos.balance <= 0:
            return opts

        open_k_date = pos.info["__open_k_date"]

        cd_d = market_data.get_cl_data(code, market_data.frequencys[1])
        k_now_d = cd_d.get_src_klines()[-1]
        k_pre_d = cd_d.get_src_klines()[-2]
        price = cd_d.get_src_klines()[-1].c
        open_next_klines = [_k for _k in cd_d.get_src_klines() if _k.date > open_k_date]

        # 跌停时记录但不平仓（无法成交），等次日开盘价确认后再处理
        dt_price = self.code_dt_price(code, k_pre_d.c)
        if dt_price - 0.01 <= price <= dt_price + 0.01:
            pos.info["__dt_price"] = dt_price
            return opts

        is_day_close = True
        if self.mode != "test":
            now_datetime = datetime.datetime.now()
            if now_datetime.hour == 14 and now_datetime.minute >= 50:
                is_day_close = True
            else:
                is_day_close = False

        # 前日曾跌停且今日开盘低于跌停价，立即以开盘价平仓（不受收盘限制）
        if True:
            if "__dt_price" in pos.info.keys() and k_now_d.o < pos.info["__dt_price"]:
                opts.append(
                    Operation(
                        code,
                        "sell",
                        mmd,
                        loss_price=k_now_d.o,  # 指定以开盘价平仓
                        msg="之前有跌停，当前价格小于跌停价格",
                        close_uid="跌停平仓",
                    )
                )

        # 跳空低开（开盘价低于昨日最低）立即止损，不等收盘（不受收盘限制）
        if True:
            if k_now_d.o < k_pre_d.l:
                opts.append(
                    Operation(
                        code,
                        "sell",
                        mmd,
                        loss_price=k_now_d.o,  # 指定以开盘价平仓
                        msg="跳空低开，直接止损平仓",
                        close_uid="跳空低开",
                    )
                )

        if is_day_close is False:
            return opts

        # 以下条件仅在收盘时刻检查

        loss_opt = self.check_loss(mmd, pos, price)
        if loss_opt is not None:
            opts.append(loss_opt)

        # 移动止损：开仓后次日起，收盘价跌破昨日最低即止损（趋势跟踪）
        if True:
            if len(open_next_klines) >= 1 and k_now_d.c < k_pre_d.l:
                opts.append(
                    Operation(
                        code,
                        "sell",
                        mmd,
                        msg="移动止损，当前收盘价格，低于昨日最低价格",
                        close_uid="移动止损",
                    )
                )

        # 日线顶分型确认后，收盘价跌破分型中间K线低点，止损
        if True:
            bi_d = self.last_done_bi(cd_d.get_bis())
            if (
                is_day_close
                and bi_d.type == "up"
                and bi_d.end.k.date > open_k_date
                and price < bi_d.end.k.l
            ):
                opts.append(
                    Operation(
                        code,
                        "sell",
                        mmd,
                        msg=f"当前价格，低于日线顶分型中间k线低点 {bi_d.end.k.l}",
                        close_uid="低于日线顶分型",
                    )
                )

        # 持仓期间曾站上均线后，阴线跌破均线则止损（确认持仓期间价格曾有效突破均线才启用）
        if True:
            if k_now_d.c < k_now_d.o:
                idx_ma5 = self.idx_ma(cd_d, 5)
                if price > idx_ma5[-1]:
                    pos.info["__gt_idx_ma5"] = 1
                if (
                    "__gt_idx_ma5" in pos.info.keys()
                    and k_now_d.c < k_now_d.o
                    and k_now_d.c < idx_ma5[-1]
                ):
                    opts.append(
                        Operation(
                            code,
                            "sell",
                            mmd,
                            msg=f"低开阴线，并且价格小于5日均线： {round(idx_ma5[-1], 2)}",
                            close_uid="低于5日均线",
                        )
                    )
        if True:
            if k_now_d.c < k_now_d.o:
                idx_ma10 = self.idx_ma(cd_d, 10)
                if price > idx_ma10[-1]:
                    pos.info["__gt_idx_ma10"] = 1
                if (
                    "__gt_idx_ma10" in pos.info.keys()
                    and k_now_d.c < k_now_d.o
                    and k_now_d.c < idx_ma10[-1]
                ):
                    opts.append(
                        Operation(
                            code,
                            "sell",
                            mmd,
                            msg=f"低开阴线，并且价格小于10日均线： {round(idx_ma10[-1], 2)}",
                            close_uid="低于10日均线",
                        )
                    )
        if True:
            if k_now_d.c < k_now_d.o:
                idx_ma20 = self.idx_ma(cd_d, 20)
                if price > idx_ma20[-1]:
                    pos.info["__gt_idx_ma20"] = 1
                if (
                    "__gt_idx_ma20" in pos.info.keys()
                    and k_now_d.c < k_now_d.o
                    and k_now_d.c < idx_ma20[-1]
                ):
                    opts.append(
                        Operation(
                            code,
                            "sell",
                            mmd,
                            msg=f"低开阴线，并且价格小于20日均线： {round(idx_ma20[-1], 2)}",
                            close_uid="低于20日均线",
                        )
                    )

        # 从开仓后最高价回调超过阈值时分档止盈（5%/10%/15%/20%/30%/50%），同时触发多个时均记录
        if True and len(open_next_klines) > 0:
            nex_k_high = max([_k.h for _k in open_next_klines])
            nex_k_callback_rate = (price - nex_k_high) / nex_k_high * 100
            if nex_k_callback_rate <= -5:
                opts.append(
                    Operation(
                        code,
                        "sell",
                        mmd,
                        msg=f"最高价格 {nex_k_high} 回调 ({nex_k_callback_rate}) -5%，止盈",
                        close_uid="利润回调5%",
                    )
                )
            if nex_k_callback_rate <= -10:
                opts.append(
                    Operation(
                        code,
                        "sell",
                        mmd,
                        msg=f"最高价格 {nex_k_high} 回调 ({nex_k_callback_rate}) -10%，止盈",
                        close_uid="利润回调10%",
                    )
                )
            if nex_k_callback_rate <= -15:
                opts.append(
                    Operation(
                        code,
                        "sell",
                        mmd,
                        msg=f"最高价格 {nex_k_high} 回调 ({nex_k_callback_rate}) -15%，止盈",
                        close_uid="利润回调15%",
                    )
                )
            if nex_k_callback_rate <= -20:
                opts.append(
                    Operation(
                        code,
                        "sell",
                        mmd,
                        msg=f"最高价格 {nex_k_high} 回调 ({nex_k_callback_rate}) -20%，止盈",
                        close_uid="利润回调20%",
                    )
                )
            if nex_k_callback_rate <= -30:
                opts.append(
                    Operation(
                        code,
                        "sell",
                        mmd,
                        msg=f"最高价格 {nex_k_high} 回调 ({nex_k_callback_rate}) -30%，止盈",
                        close_uid="利润回调30%",
                    )
                )
            if nex_k_callback_rate <= -50:
                opts.append(
                    Operation(
                        code,
                        "sell",
                        mmd,
                        msg=f"最高价格 {nex_k_high} 回调 ({nex_k_callback_rate}) -50%，止盈",
                        close_uid="利润回调50%",
                    )
                )

        bi_d = self.last_done_bi(cd_d.get_bis())

        # 向上笔出现盘整/趋势背驰或卖点，且笔停顿，为完整平仓信号
        if (
            bi_d.type == "up"
            and bi_d.end.k.date > open_k_date
            and (bi_d.bc_exists(["pz", "qs"], "|") or len(bi_d.line_mmds("|")) > 0)
            and self.bi_td(bi_d, cd_d)
        ):
            opts.append(
                Operation(
                    code,
                    "sell",
                    mmd,
                    msg=f"向上笔盘整({bi_d.line_bcs('|')}) 或卖点 ({bi_d.line_mmds('|')})，卖出",
                )
            )

        return opts

    def code_zt_price(self, code: str, yester_price: float):
        """
        根据昨日收盘价，计算今天的涨停价格
        """
        zt_rate = 1.10
        if code.split(".")[1][0] in ["3"]:
            zt_rate = 1.20
        zt_price = round(yester_price * zt_rate, 2)
        return zt_price

    def code_dt_price(self, code: str, yester_price: float):
        """
        根据昨日收盘价，计算今天的跌停价格
        """
        zt_rate = 0.90
        if code.split(".")[1][0] in ["3"]:
            zt_rate = 0.80
        zt_price = round(yester_price * zt_rate, 2)
        return zt_price


if __name__ == "__main__":
    import pandas as pd

    from chanlun.backtesting import backtest
    from chanlun.cl_utils import query_cl_chart_config
    from chanlun.exchange.exchange_tdx import ExchangeTDX

    # 获取沪深A股全量代码（通达信格式）并转换为掘金格式
    ex = ExchangeTDX()
    stocks = ex.all_stocks()
    run_codes = [
        _s["code"] for _s in stocks if _s["code"][0:5] in ["SH.60", "SZ.00", "SZ.30"]
    ]
    run_codes = [_c.replace("SH.", "SHSE.").replace("SZ.", "SZSE.") for _c in run_codes]
    print(f"回测代码数量：{len(run_codes)}")

    cl_config = query_cl_chart_config("a", "SHSE.000001")
    bt_config = {
        "save_file": str(get_data_path() / "backtest" / "a_d_mmd_v0_signal.pkl"),
        "strategy": StrategyADMMDTest("test"),
        # signal 模式：固定金额开仓，用于信号统计；trade 模式：按实际资金开仓
        "mode": "signal",
        "market": "a",
        "base_code": "SHSE.000001",
        "codes": run_codes,
        # frequencys[0]=周线（大级别背景），frequencys[1]=日线（信号级别）
        "frequencys": ["w", "d"],
        "start_datetime": "2020-01-01 00:00:00",
        "end_datetime": "2024-06-01 00:00:00",
        "init_balance": 1000000,
        "fee_rate": 0.001,
        "max_pos": 8,
        "cl_config": cl_config,
    }

    BT = backtest.BackTest(bt_config)

    BT.run_process(max_workers=5)
    BT.save()
    BT.result()
    print("Done")
