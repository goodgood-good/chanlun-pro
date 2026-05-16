from typing import Dict, List, Union

from chanlun.backtesting.backtest import BackTest
from chanlun.backtesting.base import POSITION, MarketDatas, Operation, Strategy
from chanlun.cl_interface import ICL


class StrategyAXDTradeModel(Strategy):
    """
    市场：股票市场
    周期：多周期（推荐 d/30m/5m）

    线段交易模式，搏一搏线段的转折

    """

    def __init__(self):
        super().__init__()

        self._run_codes = []
        self._date_day_key = 0
        self._trade_infos = {
            "SHSE.60": {
                "code": "SHSE.000001",
                "name": "上证指数",
                "trade": True,
                "trade_days": [],
            },
            "SHSE.68": {
                "code": "SHSE.000688",
                "name": "科创板块",
                "trade": True,
                "trade_days": [],
            },
            "SZSE.00": {
                "code": "SZSE.399001",
                "name": "深证成指",
                "trade": True,
                "trade_days": [],
            },
            "SZSE.30": {
                "code": "SZSE.399006",
                "name": "创业板指",
                "trade": True,
                "trade_days": [],
            },
        }

        self._index_cds: Dict[str, ICL] = {}

        self._max_loss_rate = 10

    def on_bt_loop_start(self, bt: BackTest):
        """每日开始时预筛选候选代码，避免对全量标的运行完整策略逻辑。"""
        # 同一天只执行一次，通过日期 key 去重
        if bt.datas.now_date.day == self._date_day_key:
            return True
        self._date_day_key = bt.datas.now_date.day

        # 初步过滤：只保留当前线段向下且已有买点或盘整背驰的标的
        self._run_codes = []
        for code in bt.codes:
            if self._trade_infos[code[:7]]["trade"] is False:
                continue
            cd = bt.datas.get_cl_data(code, bt.frequencys[0], bt.cl_config)
            if len(cd.get_xds()) > 0:
                xd_zss = cd.get_xd_zss()
                xd = cd.get_xds()[-1]
                if xd.type == "down" and (
                    xd.mmd_exists(["1buy", "2buy", "3buy"]) or xd.bc_exists(["pz"])
                ):
                    # 三买须有至少两个线段中枢作背景，否则可能是趋势延续而非反转
                    if xd.mmd_exists(["3buy"]) and len(xd_zss) < 2:
                        continue
                    self._run_codes.append(code)
        # 已持仓标的无论筛选结果如何，均须纳入以便执行平仓检查
        self._run_codes += bt.trader.position_codes()
        return True

    def open(
        self, code, market_data: MarketDatas, poss: Dict[str, POSITION]
    ) -> List[Operation]:
        opts = []

        if code not in self._run_codes:
            return opts

        cd_day = market_data.get_cl_data(code, market_data.frequencys[0])

        if len(cd_day.get_xds()) == 0:
            return opts
        xd_day = cd_day.get_xds()[-1]
        # 过滤不符合要求的线段
        if xd_day.type == "up":
            return opts
        if len(xd_day.line_mmds()) == 0 and len(xd_day.line_bcs()) == 0:
            return opts

        cd_30m = market_data.get_cl_data(code, market_data.frequencys[1])
        cd_5m = market_data.get_cl_data(code, market_data.frequencys[2])

        if len(cd_30m.get_bis()) == 0 or len(cd_5m.get_bis()) == 0:
            return opts

        price = cd_5m.get_klines()[-1].c

        # 日线向下笔已完成，且当前价格已突破笔结束分型最后K线高点（确认反弹启动）
        bi_day = cd_day.get_bis()[-1]
        if (
            bi_day.type == "down"
            and bi_day.is_done()
            and price > bi_day.end.klines[-1].h
        ):
            pass
        else:
            return opts

        # 笔距离线段结束笔不超过4笔，防止入场时机过晚
        if bi_day.index - xd_day.end_line.index > 4:
            return opts

        # 次级别结构验证：日线笔对应 30m 至少1段，或 5m 至少5段（确保次级别有完整走势）
        xds_30m = [
            _xd
            for _xd in cd_30m.get_xds()
            if _xd.start.k.date >= bi_day.start.k.date
            and _xd.end.k.date <= bi_day.end.k.klines[-1].date
        ]
        xds_5m = [
            _xd
            for _xd in cd_5m.get_xds()
            if _xd.start.k.date >= bi_day.start.k.date
            and _xd.end.k.date <= bi_day.end.k.klines[-1].date
        ]
        if len(xds_30m) >= 1 or len(xds_5m) >= 5:
            pass
        else:
            return opts

        xds_30m = [
            _xd for _xd in cd_30m.get_xds() if _xd.start.k.date >= xd_day.start.k.date
        ]
        if len(xds_30m) < 3:
            # 30m 线段不满足条件
            return opts

        # 5m 级别次级别入场确认：只接受第一次出现的买点，防止重复信号
        is_ok_5m = False
        low_level_5m_msg = ""

        # 只做第一个三买，出现多个三买说明走势已延续，不再是初始反转
        if is_ok_5m is False:
            bis_5m_3buy = [
                _bi
                for _bi in cd_5m.get_bis()
                if _bi.start.k.date >= bi_day.end.k.date
                and _bi.mmd_exists(["3buy"], "|")
            ]
            if len(bis_5m_3buy) == 1:
                is_ok_5m = True
                low_level_5m_msg = "5m 三买点"

        # 只做第一个一买
        if is_ok_5m is False:
            bis_5m_1buy = [
                _bi
                for _bi in cd_5m.get_bis()
                if _bi.start.k.date >= bi_day.end.k.date
                and _bi.mmd_exists(["1buy"], "|")
            ]
            if len(bis_5m_1buy) == 1:
                is_ok_5m = True
                low_level_5m_msg = "5m 一买点"
        # 只做第一个二买
        if is_ok_5m is False:
            bis_5m_2buy = [
                _bi
                for _bi in cd_5m.get_bis()
                if _bi.start.k.date >= bi_day.end.k.date
                and _bi.mmd_exists(["2buy"], "|")
            ]
            if len(bis_5m_2buy) == 1:
                is_ok_5m = True
                low_level_5m_msg = "5m 二买点"

        if is_ok_5m is False:
            return opts
        # 止损取日/30m/5m 三个级别最低点中的最低值，再按最大止损比例收窄
        bi_day = cd_day.get_bis()[-1]
        bi_30m = cd_30m.get_bis()[-1]
        bi_5m = cd_5m.get_bis()[-1]
        stop_loss_price = min([bi_day.low, bi_30m.low, bi_5m.low])
        stop_loss_price = self.get_max_loss_price(
            "buy", price, stop_loss_price, self._max_loss_rate
        )

        # 低级别线段评分：30m 和 5m 各贡献 1 分，须同时有背驰或买点支撑才入场（score >= 2）
        xds_down_30m = [
            _xd
            for _xd in cd_30m.get_xds()
            if _xd.start.k.date >= xd_day.start.k.date and _xd.type == "down"
        ]
        xds_down_5m = [
            _xd
            for _xd in cd_5m.get_xds()
            if _xd.start.k.date >= xd_day.start.k.date and _xd.type == "down"
        ]

        score_val = 0
        for _xd in xds_down_30m[-2:]:
            if _xd.mmd_exists(["1buy", "2buy", "3buy"]) or _xd.bc_exists(
                ["xd", "pz", "qs"]
            ):
                score_val += 1
                break
        for _xd in xds_down_5m[-4:]:
            if _xd.mmd_exists(["1buy", "2buy", "3buy"]) or _xd.bc_exists(
                ["xd", "pz", "qs"]
            ):
                score_val += 1
                break

        if score_val < 2:
            return opts

        info = {
            "day_xd_type": (
                0 if len(cd_day.get_xds()) == 0 else cd_day.get_xds()[-1].type
            ),
            "day_bi": f"{bi_day.type}_{bi_day.is_done()}",
            "xd_start_date": xd_day.start.k.date,
            "xd_end_date": xd_day.end.k.date,
            "bi_start_date": bi_day.start.k.date,
            "bi_end_date": bi_day.end.k.date,
            "open_buy_date": cd_day.get_src_klines()[-1].date,
            "xd_30m_num": len(xds_30m),
            "zs_juli_rate": 0,
            "zs_type": "",
            "zs_one_line_type": "",
            "zs_line_num": 0,
            "is_pause_loss": 0,
            "score_val": score_val,
            "low_5m_msg": low_level_5m_msg,
        }

        for mmd in xd_day.get_mmds():
            if mmd.name == "3buy":
                # 中枢超过9段说明中枢已过于成熟，三买可靠性下降，跳过
                if mmd.zs.line_num > 9:
                    continue
                info["zs_juli_rate"] = (price - mmd.zs.zg) / mmd.zs.zg * 100
            else:
                info["zs_juli_rate"] = (mmd.zs.zd - price) / price * 100
            info["zs_type"] = mmd.zs.type
            info["zs_one_line_type"] = mmd.zs.lines[0].type
            info["zs_line_num"] = mmd.zs.line_num
            return [
                Operation(
                    code=code,
                    opt="buy",
                    mmd=mmd.name,
                    loss_price=stop_loss_price,
                    info=info,
                    msg=f"线段买卖点 {xd_day.line_mmds()}，{low_level_5m_msg}，{score_val}，止损价格 {stop_loss_price}",
                )
            ]
        for bc in xd_day.get_bcs():
            if bc.type != "pz":
                continue
            bc_mmd = f"down_{bc.type}_bc_buy"
            info["zs_juli_rate"] = (bc.zs.zd - price) / price * 100
            info["zs_type"] = bc.zs.type
            info["zs_one_line_type"] = bc.zs.lines[0].type
            info["zs_line_num"] = bc.zs.line_num
            return [
                Operation(
                    code=code,
                    opt="buy",
                    mmd=bc_mmd,
                    loss_price=stop_loss_price,
                    info=info,
                    msg=f"线段背驰 {xd_day.line_bcs()}，{low_level_5m_msg}，{score_val}，止损价格 {stop_loss_price}",
                )
            ]

        return opts

    def close(
        self, code, mmd: str, pos: POSITION, market_data: MarketDatas
    ) -> Union[Operation, None, List[Operation]]:
        """
        看大做小
        日线出现顶分型，并且收盘价小于5日均线
        30m级别三段，出现卖点
        5m级别出现三类卖点
        """
        cd_day = market_data.get_cl_data(code, market_data.frequencys[0])
        cd_30m = market_data.get_cl_data(code, market_data.frequencys[1])
        cd_5m = market_data.get_cl_data(code, market_data.frequencys[2])

        price = cd_5m.get_klines()[-1].c
        opt = self.check_loss(mmd, pos, price)
        if opt is not None:
            return opt

        info = pos.info
        ma5_day = self.idx_ma(cd_day, 5)[-1]
        last_day_date = cd_day.get_klines()[-1].date
        bi_day = self.last_done_bi(cd_day.get_bis())
        if (
            bi_day.type == "up"
            and bi_day.is_done()
            and last_day_date > bi_day.end.klines[-1].klines[-1].date
        ):
            if price < bi_day.end.klines[-1].l and price < ma5_day:
                # 确认日线向上笔结构有效（30m >= 1段 或 5m >= 5段），再触发平仓
                xds_30m = [
                    _xd
                    for _xd in cd_30m.get_xds()
                    if _xd.start.k.date >= bi_day.start.k.date
                    and _xd.end.k.date <= bi_day.end.k.klines[-1].date
                ]
                xds_5m = [
                    _xd
                    for _xd in cd_5m.get_xds()
                    if _xd.start.k.date >= bi_day.start.k.date
                    and _xd.end.k.date <= bi_day.end.k.klines[-1].date
                ]
                if len(xds_30m) >= 1 or len(xds_5m) >= 5:
                    return Operation(
                        code=code,
                        opt="sell",
                        mmd=mmd,
                        msg=f"日线向上笔结束(30m:{len(xds_30m)}/5m:{len(xds_5m)})，并且价格小于5日均线，平仓退出",
                    )

        # 30m 走势至少完成3段后，出现卖点即平仓（等待走势充分展开）
        xds_30m = [
            _xd for _xd in cd_30m.get_xds() if _xd.start.k.date >= info["bi_end_date"]
        ]
        if len(xds_30m) >= 3:
            bi_30m = self.last_done_bi(cd_30m.get_bis())
            if (
                bi_30m.start.k.date > info["open_buy_date"]
                and bi_30m.mmd_exists(["1sell", "2sell", "3sell"])
                and self.bi_td(bi_30m, cd_30m)
            ):
                return Operation(
                    code=code,
                    opt="sell",
                    mmd=mmd,
                    msg=f"30m级别出现卖点 {bi_30m.line_mmds()}",
                )

        # 5m 走势满5段后，日线顶分型破位时出现三卖，退出
        bi_day = cd_day.get_bis()[-1]
        xds_5m = [
            _xd for _xd in cd_5m.get_xds() if _xd.start.k.date >= info["bi_end_date"]
        ]
        if (
            len(xds_5m) >= 5
            and bi_day.type == "up"
            and bi_day.is_done()
            and price < bi_day.end.klines[-1].l
        ):
            bis_5m = [
                _bi
                for _bi in cd_5m.get_bis()
                if _bi.start.k.date >= bi_day.end.klines[0].klines[0].date
                and _bi.type == "up"
            ]
            for _bi in bis_5m:
                if _bi.mmd_exists(["3sell"], "|") and self.bi_td(_bi, cd_5m):
                    return Operation(
                        code=code, opt="sell", mmd=mmd, msg="5m级别，出现笔的三类卖点"
                    )

        return False
