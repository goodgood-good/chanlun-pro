from chanlun.backtesting.base import *
from chanlun.cl_analyse import MultiLevelAnalyse
from chanlun.cl_interface import Config
from chanlun.cl_utils import cal_zs_macd_infos


class StrategyA3mmd(Strategy):
    """
    https://zhuanlan.zhihu.com/p/499188628
    根据以上文章，写的当前策略
    多周期（高低两个）策略，例如 [d, 30m]
    只做三类买卖点
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
            len(high_data.get_bis()) == 0
            or len(high_data.get_bi_zss()) < 2
            or len(high_data.get_xds()) == 0
        ):
            return opts

        # 第一个条件：中枢要求1，第一个中枢
        # 根据中枢类型的不同，判断是否第一个中枢的方法也不同
        high_config = high_data.get_config()
        if Config.ZS_TYPE_DN.value in high_config["zs_bi_type"]:
            # 段内中枢：确保当前线段内只有一个笔中枢，即走势处于第一个中枢阶段
            high_xd = high_data.get_xds()[-1]
            high_xd_bi_zss = [
                _zs
                for _zs in high_data.get_bi_zss()
                if _zs.start.index > high_xd.start.index
            ]
            if len(high_xd_bi_zss) != 1:
                return opts
        elif Config.ZS_TYPE_BZ.value in high_config["zs_bi_type"]:
            # 标准中枢延伸模式：连续两个同向中枢意味着趋势延续，不符合三类买卖点前提
            high_bi_zs_1 = high_data.get_bi_zss()[-1]
            high_bi_zs_2 = high_data.get_bi_zss()[-2]
            if high_bi_zs_2.lines[1].type == high_bi_zs_1.lines[1].type:
                return opts
        else:
            raise Exception("缠论配置，笔中枢类型错误")

        high_zs = high_data.get_bi_zss()[-1]

        # 对称中枢要求：振幅 >= 35% 才认定为有效震荡中枢（宽松阈值）
        if high_zs.zf() < 35:
            return opts

        # 级别定位：中枢内笔数 <= 7，防止中枢级别过高导致三类买卖点失效
        if len(high_zs.lines) > 7:
            return opts
        # 中枢须有 MACD 零轴回抽（dif 穿零）或至少两次金/死叉，确认走势完整性
        zs_macd_infos = cal_zs_macd_infos(high_zs, high_data)
        if (
            zs_macd_infos.dif_down_cross_num > 0 or zs_macd_infos.dif_up_cross_num > 0
        ) or (zs_macd_infos.die_cross_num >= 2 or zs_macd_infos.gold_cross_num >= 2):
            pass
        else:
            return opts

        high_bi = self.last_done_bi(high_data.get_bis())
        price = high_data.get_klines()[-1].c

        # 进入中枢前的反向笔/线段若已背驰，说明对应走势已结束，三类买卖点不成立
        high_up_bi = high_data.get_bis()[high_bi.index - 1]
        high_xd = high_data.get_xds()[-1]
        if high_up_bi.bc_exists(["bi", "pz", "qs"]) or (
            high_xd.type != high_bi.type and high_xd.bc_exists(["xd", "pz", "qs"])
        ):
            return opts

        # 止损点放在分型第三根缠论K线的高/低点
        if self._max_loss_rate is not None:
            if high_bi.type == "down":
                loss_price = price - (price * (abs(self._max_loss_rate) / 100))
                loss_price = max(loss_price, high_bi.end.klines[-1].l)
            else:
                loss_price = price + (price * (abs(self._max_loss_rate) / 100))
                loss_price = min(loss_price, high_bi.end.klines[-1].h)
        else:
            if high_bi.type == "down":
                loss_price = high_bi.end.klines[-1].l
            else:
                loss_price = high_bi.end.klines[-1].h

        if high_bi.mmd_exists(["3buy", "3sell"]):
            # 本级别三类买卖点：要求至少满足"笔强停顿/验证分型/低级别背驰"之一，避免假突破
            mla = MultiLevelAnalyse(
                high_data, market_data.get_cl_data(code, market_data.frequencys[1])
            )
            low_qs = mla.low_level_qs(high_bi, "bi")
            for mmd in high_bi.line_mmds():
                btd = self.bi_qiang_td(high_bi, high_data)
                yzfx = self.bi_yanzhen_fx(high_bi, high_data)
                low_bc = (low_qs.pz_bc or low_qs.qs_bc) and self.bi_td(
                    high_bi, high_data
                )
                if btd or yzfx or low_bc:
                    opts.append(
                        Operation(
                            code=code,
                            opt="buy",
                            mmd=mmd,
                            loss_price=loss_price,
                            info={
                                "high_bi": high_bi,
                                "high_zs": high_zs,
                            },
                            msg="买入条件：本级别买点（%s 笔停顿 %s 验证分型 %s 低级别背驰 %s）,止损价格 %s"
                            % (mmd, btd, yzfx, low_bc, loss_price),
                        )
                    )
        elif high_zs.done is False:
            # 中枢未完成时，寻找次级别强势分型形成的类三类买卖点（b-A 走法）
            # 若最后一笔已突破中枢极值（gg/dd），说明中枢可能被突破，暂不入场
            if (high_bi.type == "up" and high_zs.gg > high_bi.high) or (
                high_bi.type == "down" and high_zs.dd < high_bi.low
            ):
                return opts

            # 在笔起点之后，找同类型（与笔起点分型一致，即方向相反）且力度 >= 2 的强势分型
            high_max_fxs = [
                _fx
                for _fx in high_data.get_fxs()
                if (
                    _fx.done
                    and _fx.index > high_bi.start.index
                    and _fx.type == high_bi.start.type
                    and _fx.ld() >= 2
                )
            ]
            if len(high_max_fxs) == 0:
                return opts

            mmd = None
            # 止损设置在强势分型第三K线极值，此处未参考 _max_loss_rate，类三买卖点容忍度更大
            if (
                high_bi.type == "up"
                and high_max_fxs[-1].val > high_zs.zg
                and price
                > high_max_fxs[-1].high(
                    high_data.get_config()["fx_qj"], high_data.get_config()["fx_qy"]
                )
            ):
                mmd = "l3buy"
                loss_price = high_max_fxs[-1].klines[-1].l
            elif (
                high_bi.type == "down"
                and high_max_fxs[-1].val < high_zs.zd
                and price
                < high_max_fxs[-1].low(
                    high_data.get_config()["fx_qj"], high_data.get_config()["fx_qy"]
                )
            ):
                mmd = "l3sell"
                loss_price = high_max_fxs[-1].klines[-1].h
            if mmd:
                opts.append(
                    Operation(
                        code=code,
                        opt="buy",
                        mmd=mmd,
                        loss_price=loss_price,
                        info={"high_bi": high_bi, "high_zs": high_zs},
                        msg="买入条件：次级别强势反向分型买点（%s）,止损价格 %s"
                        % (mmd, loss_price),
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

        low_data = market_data.get_cl_data(code, market_data.frequencys[1])

        high_bi = self.last_done_bi(high_data.get_bis())
        low_bi = self.last_done_bi(low_data.get_bis())

        # 均线周期由缠论配置决定，此处固定取 5 周期均线
        idx_ma = self.idx_ma(high_data, 5)[-1]

        # 走势加速卖出：笔角度 > 50 且收盘破均线，用笔角度代替均线角度（计算更简便）
        if (
            "buy" in mmd
            and high_bi.type == "up"
            and high_bi.is_done()
            and abs(high_bi.jiaodu()) > 50
            and price < idx_ma
        ):
            return Operation(
                code=code, opt="sell", mmd=mmd, msg="笔角度大于50并且当前价格低于均线"
            )
        if (
            "sell" in mmd
            and high_bi.type == "down"
            and high_bi.is_done()
            and abs(high_bi.jiaodu()) > 50
            and price > idx_ma
        ):
            return Operation(
                code=code, opt="sell", mmd=mmd, msg="笔角度大于50并且当前价格高于均线"
            )

        # 次级别盘整/趋势背驰平仓：高级别笔停顿 + 低级别背驰 + 低级别笔停顿，三重确认避免假信号
        mla = MultiLevelAnalyse(high_data, low_data)
        low_qs = mla.low_level_qs(high_bi, "bi")
        if (
            "buy" in mmd
            and high_bi.type == "up"
            and self.bi_td(high_bi, high_data)
            and (low_qs.pz_bc or low_qs.qs_bc)
            and low_bi.td
        ):
            return Operation(
                code=code,
                opt="sell",
                mmd=mmd,
                msg="次级别背驰 %s" % ([low_qs.pz_bc, low_qs.qs_bc]),
            )

        if (
            "sell" in mmd
            and high_bi.type == "down"
            and self.bi_td(high_bi, high_data)
            and (low_qs.pz_bc or low_qs.qs_bc)
            and low_bi.td
        ):
            return Operation(
                code=code,
                opt="sell",
                mmd=mmd,
                msg="次级别背驰 %s" % ([low_qs.pz_bc, low_qs.qs_bc]),
            )

        # 小转大平仓：高级别验证分型出现即离场，防止不标准走势延伸带来的回撤
        if (
            "buy" in mmd
            and high_bi.type == "up"
            and high_bi.is_done()
            and self.bi_yanzhen_fx(high_bi, high_data)
        ):
            return Operation(code, "sell", mmd, msg="高级别验证分型平仓")
        if (
            "sell" in mmd
            and high_bi.type == "down"
            and high_bi.is_done()
            and self.bi_yanzhen_fx(high_bi, high_data)
        ):
            return Operation(code, "sell", mmd, msg="高级别验证分型平仓")

        # 高级别笔背驰平仓
        if (
            "buy" in mmd
            and high_bi.type == "up"
            and self.bi_td(high_bi, high_data)
            and high_bi.bc_exists(["bi", "pz", "qs"])
        ):
            return Operation(
                code, "sell", mmd, msg="高级别笔背驰（%s）" % high_bi.line_bcs()
            )
        if (
            "sell" in mmd
            and high_bi.type == "down"
            and self.bi_td(high_bi, high_data)
            and high_bi.bc_exists(["bi", "pz", "qs"])
        ):
            return Operation(
                code, "sell", mmd, msg="高级别笔背驰（%s）" % high_bi.line_bcs()
            )

        # 低级别笔出现一/二类买卖点时，高级别笔也已完成，视为趋势转折信号
        if (
            "buy" in mmd
            and low_bi.mmd_exists(["1sell", "2sell"])
            and high_bi.type == "up"
            and high_bi.is_done()
        ):
            return Operation(
                code, "sell", mmd, msg="低级别笔卖点（%s）" % low_bi.line_mmds()
            )
        if (
            "sell" in mmd
            and low_bi.mmd_exists(["1buy", "2buy"])
            and high_bi.type == "down"
            and high_bi.is_done()
        ):
            return Operation(
                code, "sell", mmd, msg="低级别笔买点（%s）" % low_bi.line_mmds()
            )

        return None
