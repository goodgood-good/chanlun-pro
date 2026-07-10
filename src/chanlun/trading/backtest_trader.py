import copy
import datetime
import time
from typing import Dict, List, Tuple

from chanlun import fun
from chanlun import utils
from chanlun.trading import futures_contracts
from chanlun.trading.base import POSITION, MarketDatas, Operation, Strategy, Trader
from chanlun.persistence.db import db
from chanlun.persistence.file_db import fdb


class BackTestTrader(Trader):
    """
    回测交易（可继承支持实盘）
    """

    def __init__(
        self,
        name,
        mode="signal",
        market="a",
        init_balance=100000,
        fee_rate=0.0005,
        max_pos=10,
        log=None,
    ):
        """
        交易者初始化
        :param name: 交易者名称
        :param mode: 执行模式 signal 测试信号模式，固定金额开仓；trade 实际买卖模式；real 线上实盘交易
        :param market: 市场 (a 沪深 us 美股  hk 香港  currency 数字货币 futures 期货)
        :param init_balance: 初始资金
        :param fee_rate: 手续费比例
        """

        # 策略基本信息
        self.name = name
        self.mode = mode
        self.market = market

        self.can_close_today: bool = False  # 是否可以平今
        self.can_short: bool = False  # 是否可以做空

        if self.market == "a":
            self.can_close_today = False
            self.can_short = False
        if self.market == "us":
            self.can_close_today = True
            self.can_short = False
        if self.market == "hk":
            self.can_close_today = True
            self.can_short = False
        if self.market == "currency":
            self.can_close_today = True
            self.can_short = True
        if self.market == "futures":
            self.can_close_today = True
            self.can_short = True

        self.allow_mmds = None

        # 资金情况
        self.balance = init_balance if mode == "trade" else 0
        self.fee_rate = fee_rate
        self.fee_total = 0
        self.max_pos = max_pos

        # 单次仓位最大占用资金，避免资金成指数基本扩展，后续市场深度无法提供大资金买入
        self.max_single_pos_balance = None

        # ---- M5 风控上限 (online 生效, None=不限, 默认与现状完全一致) ----
        self.max_single_notional = None   # 单笔最大名义金额
        self.max_total_notional = None    # 组合总名义上限
        self.min_order_notional = None    # 最小下单名义额 (防超小单被拒)
        self.daily_max_loss = None        # 当日最大亏损 (达到则停止开仓), 预留, 暂未接入

        # 是否打印日志
        self.log = log
        self.log_history = []

        # 时间统计
        self.use_times = {
            "strategy_close": 0,
            "strategy_open": 0,
            "execute": 0,
            "position_record": 0,
        }

        self.futures_contracts = futures_contracts.futures_contracts

        # 策略对象
        self.strategy: Strategy = None

        # 回测数据对象
        self.datas: MarketDatas = None

        # 当前持仓信息
        self.positions: Dict[str, POSITION] = {}
        self.positions_history: Dict[str, List[POSITION]] = {}
        # 持仓资金历史
        self.positions_balance_history: Dict[str, Dict[str, float]] = {}
        # 持仓盈亏记录
        self.hold_profit_history = {}
        # 资产历史
        self.balance_history: Dict[str, float] = {}

        # 代码订单信息
        self.orders = {}

        # 统计结果数据
        self.results = {
            "1buy": {"win_num": 0, "loss_num": 0, "win_balance": 0, "loss_balance": 0},
            "2buy": {"win_num": 0, "loss_num": 0, "win_balance": 0, "loss_balance": 0},
            "l2buy": {"win_num": 0, "loss_num": 0, "win_balance": 0, "loss_balance": 0},
            "3buy": {"win_num": 0, "loss_num": 0, "win_balance": 0, "loss_balance": 0},
            "l3buy": {"win_num": 0, "loss_num": 0, "win_balance": 0, "loss_balance": 0},
            "down_bi_bc_buy": {
                "win_num": 0,
                "loss_num": 0,
                "win_balance": 0,
                "loss_balance": 0,
            },
            "down_xd_bc_buy": {
                "win_num": 0,
                "loss_num": 0,
                "win_balance": 0,
                "loss_balance": 0,
            },
            "down_pz_bc_buy": {
                "win_num": 0,
                "loss_num": 0,
                "win_balance": 0,
                "loss_balance": 0,
            },
            "down_qs_bc_buy": {
                "win_num": 0,
                "loss_num": 0,
                "win_balance": 0,
                "loss_balance": 0,
            },
            "1sell": {"win_num": 0, "loss_num": 0, "win_balance": 0, "loss_balance": 0},
            "2sell": {"win_num": 0, "loss_num": 0, "win_balance": 0, "loss_balance": 0},
            "l2sell": {
                "win_num": 0,
                "loss_num": 0,
                "win_balance": 0,
                "loss_balance": 0,
            },
            "3sell": {"win_num": 0, "loss_num": 0, "win_balance": 0, "loss_balance": 0},
            "l3sell": {
                "win_num": 0,
                "loss_num": 0,
                "win_balance": 0,
                "loss_balance": 0,
            },
            "up_bi_bc_sell": {
                "win_num": 0,
                "loss_num": 0,
                "win_balance": 0,
                "loss_balance": 0,
            },
            "up_xd_bc_sell": {
                "win_num": 0,
                "loss_num": 0,
                "win_balance": 0,
                "loss_balance": 0,
            },
            "up_pz_bc_sell": {
                "win_num": 0,
                "loss_num": 0,
                "win_balance": 0,
                "loss_balance": 0,
            },
            "up_qs_bc_sell": {
                "win_num": 0,
                "loss_num": 0,
                "win_balance": 0,
                "loss_balance": 0,
            },
        }

        # 记录持仓盈亏、资金历史的格式化日期形式
        self.record_dt_format = "%Y-%m-%d %H:%M:%S"

        # 在执行策略前，手动指定执行的开始时间
        self.begin_run_dt: datetime.datetime = None

        # 缓冲区的执行操作，用于在特定时间点批量进行开盘检测后，对要执行的开盘信号再次进行过滤筛选
        self.buffer_opts: List[Operation] = []

    def add_times(self, key, ts):
        if key not in self.use_times.keys():
            self.use_times[key] = 0

        self.use_times[key] += ts
        return True

    def set_strategy(self, _strategy: Strategy):
        """
        设置策略对象
        :param _strategy:
        :return:
        """
        self.strategy = _strategy

    def set_data(self, _data: MarketDatas):
        """
        设置数据对象
        """
        self.datas = _data

    def save_to_pkl(self, key: str):
        """
        将对象数据保存到 pkl 文件中
        """
        save_infos = {
            "name": self.name,
            "mode": self.mode,
            "market": self.market,
            "can_close_today": self.can_close_today,
            "can_short": self.can_short,
            "allow_mmds": self.allow_mmds,
            "balance": self.balance,
            "fee_rate": self.fee_rate,
            "fee_total": self.fee_total,
            "max_pos": self.max_pos,
            "positions": self.positions,
            "positions_history": self.positions_history,
            "positions_balance_history": self.positions_balance_history,
            "hold_profit_history": self.hold_profit_history,
            "balance_history": self.balance_history,
            "orders": self.orders,
            "results": self.results,
            # H3-c: CTP order_ref 持久化, 重启后恢复避免撞号 (非 CTP trader 为 None)
            "ctp_order_ref": getattr(self, "_ctp_order_ref_snapshot", None),
        }
        if key is not None:
            # H3-b: 记录主 pkl key, 供 WAL 文件名派生
            self._pkl_key = key
            # N3: 深拷贝快照后再交异步落盘线程池, 防后台 pickle 迭代 positions/历史集合时
            # 主线程 execute 改这些容器致 `dict changed size during iteration` 被吞成 warning=账本静默丢盘。
            fdb.cache_pkl_to_file(key, copy.deepcopy(save_infos))
        return save_infos

    def load_from_pkl(self, key: str, save_infos: dict = None):
        """
        从 pkl 文件 中恢复之前的数据
        """
        # H3-b: 记录主 pkl key, 供 WAL 文件名派生 (即使本次无快照也要记, 以便后续落盘/WAL)
        self._pkl_key = key
        if save_infos is None:
            save_infos = fdb.cache_pkl_from_file(key)
            if save_infos is None:
                return False
        self.name = save_infos["name"]
        self.mode = save_infos["mode"]
        self.market = save_infos["market"] if "market" in save_infos.keys() else None
        self.can_close_today = (
            save_infos["can_close_today"]
            if "can_close_today" in save_infos.keys()
            else False
        )
        self.can_short = (
            save_infos["can_short"] if "can_short" in save_infos.keys() else False
        )
        self.allow_mmds = save_infos["allow_mmds"]
        self.balance = save_infos["balance"]
        self.fee_rate = save_infos["fee_rate"]
        self.fee_total = save_infos["fee_total"]
        self.max_pos = save_infos["max_pos"]
        self.results = save_infos["results"]

        self.positions = save_infos["positions"]
        self.positions_history = save_infos["positions_history"]
        self.positions_balance_history = save_infos["positions_balance_history"]
        self.hold_profit_history = save_infos["hold_profit_history"]
        self.balance_history = save_infos["balance_history"]
        self.orders = save_infos["orders"]

        # H3-c: 恢复 CTP order_ref 快照 (旧 pkl 无此键时为 None), 供 CTP 子类 load 覆写读取
        self._ctp_order_ref_snapshot = save_infos.get("ctp_order_ref", None)

        return True

    # ---------- H3-b: 下单意图 WAL (幂等键) ----------
    # 用途: 下单"前"先持久化开仓/平仓意图, 成交并落盘后清除。崩溃恢复时 WAL 残留的意图
    #   即"可能已成交但未落盘"的窗口, 启动对账 (reconcile_positions / H3-d) 据此向券商核对。
    # ⚠️ 调用点 (在 execute 的 open_buy 前 wal_write_intent / 落盘后 wal_clear_intent) 侵入
    #   交易热路径, 设计标注需谨慎; 本类仅提供基础设施, 具体接入点 (execute 内细粒度 vs
    #   驱动 TR.run 外粗粒度) 由投产前灰度决策, 见 audit/_round3_fix_design_exec.md H3-b。
    def _wal_key(self):
        """WAL 文件名 (与主 pkl 同 key 派生)。无 _pkl_key 时返回 None (不启用 WAL)。"""
        key = getattr(self, "_pkl_key", None)
        return f"{key}__wal" if key else None

    def wal_write_intent(self, code, opt, order_ref=None):
        """下单前持久化开仓/平仓意图 (H3-b)。崩溃恢复时据此向券商对账。

        仅 online 模式生效 (signal 回测不落 WAL); 写入失败不阻断下单
        (退化为现状, 不更危险)。
        """
        if self.mode == "signal" or self._wal_key() is None:
            return
        try:
            wal = fdb.cache_pkl_from_file(self._wal_key()) or {}
            wal[f"{code}:{opt.open_uid}:{opt.key}"] = {
                "code": code,
                "open_uid": opt.open_uid,
                "key": opt.key,
                "mmd": opt.mmd,
                "opt": opt.opt,
                "order_ref": order_ref,
                "datetime": self.get_now_datetime().isoformat(),
            }
            fdb.cache_pkl_to_file(self._wal_key(), wal)
        except Exception:
            pass  # WAL 失败退化为现状, 不抛断下单

    def wal_clear_intent(self, code, opt):
        """成交并落盘后清除意图 (H3-b)。"""
        if self.mode == "signal" or self._wal_key() is None:
            return
        try:
            wal = fdb.cache_pkl_from_file(self._wal_key()) or {}
            wal.pop(f"{code}:{opt.open_uid}:{opt.key}", None)
            fdb.cache_pkl_to_file(self._wal_key(), wal)
        except Exception:
            pass

    # ---------- M1: 券商持仓对账 ----------
    def query_broker_position(self, code):
        """查询券商持仓, 返回三态 (M1):

            ("ok", positions_obj)   查询成功 (空 dict/list 表示确认无仓)
            ("fail", None)          查询失败/异常 (不得据此清本地仓)

        子类按各自 ex.positions 语义实现; 默认 online 用 self.ex.positions。
        关键: 把"确认已平"(查询成功且空) 与"查询失败"(网络/超时/抛错) 区分开,
        后者绝不允许据此清本地持仓 (会导致裸持失管)。
        """
        try:
            return ("ok", self.ex.positions(code))
        except Exception as e:
            if self.log:
                self.log(f"{code} 持仓查询失败(不清本地): {e}")
            return ("fail", None)

    def _broker_already_holds(self, code) -> bool:
        """F1/H3 同 code 去重: 券商已持有该 code 返 True(open_* 据此跳过重复开仓)。

        仅查询成功且券商持仓非空时返 True; 查询失败返 False——不新增拦截、不因 flaky broker
        查询阻塞开仓(与 reconcile 的 fail 处理一致)。防崩溃(券商已成交、本地未落盘)重启后
        self.positions 为空但券商有仓时, 策略下一 tick 对同 code 二次开真单。
        """
        status, broker_pos = self.query_broker_position(code)
        return status == "ok" and bool(broker_pos)

    def reconcile_positions(self, codes: list):
        """以券商持仓为准, 校正本地 self.positions (M1 + H3-d)。

        - 券商有仓 / 本地无: 告警 (可能是 WAL 残留的已成交未落盘)
        - 券商无仓 / 本地有: 告警 (可能误持)
        - 查询失败: 跳过该 code, 绝不据失败清本地
        保守起见仅告警 + 落地日志, 不自动建仓/平仓 (自动改仓都有误操作风险, 交人工据告警处理)。
        这是"对账层"的最小安全形态。
        """
        for code in codes:
            status, broker_pos = self.query_broker_position(code)
            if status == "fail":
                continue
            # 本地该 code 的非空持仓
            local_has = any(
                _p.code == code and _p.amount != 0 for _p in self.positions.values()
            )
            # 券商是否有该 code 持仓 (broker_pos 为 dict/list, 空即无仓)
            broker_has = bool(broker_pos)
            if broker_has and not local_has:
                msg = f"{code} 对账差异: 券商有仓但本地无 (疑已成交未落盘), 请人工核对"
                self._safe_alert(self.market, "持仓对账", msg)
                self._print_log(f"[对账] {msg}")
            elif local_has and not broker_has:
                msg = f"{code} 对账差异: 本地有仓但券商无 (疑误持/已被平), 请人工核对"
                self._safe_alert(self.market, "持仓对账", msg)
                self._print_log(f"[对账] {msg}")

    # ---------- M5: 开仓前统一风控前置层 ----------
    def risk_precheck(self, code, opt, est_price, est_amount, est_notional):
        """开仓前统一风控 (M5)。返回 (ok: bool, reason: str)。各 open_* 真实下单前调用。

        各上限阈值默认 None → 不启用 → 返回值恒 (True, "") → 行为与现状完全一致;
        逐 trader 灰度时按需设置 max_single_notional / max_total_notional / min_order_notional。
        价格/数量非法 (NaN/非正/<=0) 始终拦截 (与阈值无关)。
        """
        if est_price is None or not (est_price == est_price and est_price > 0):
            return False, "价格非法(NaN/非正)"
        if est_amount is None or est_amount <= 0:
            return False, "数量<=0"
        if self.min_order_notional is not None and est_notional < self.min_order_notional:
            return False, f"低于最小下单额 {self.min_order_notional}"
        if self.max_single_notional is not None and est_notional > self.max_single_notional:
            return False, f"超单笔上限 {self.max_single_notional}"
        if self.max_total_notional is not None:
            cur = sum(p.balance for p in self.hold_positions())
            if cur + est_notional > self.max_total_notional:
                return False, f"超组合总上限 {self.max_total_notional}"
        return True, ""

    def get_price(self, code):
        """
        回测中方法，获取股票代码当前的价格，根据最小周期 k 线收盘价
        """
        price_info = self.datas.last_k_info(code)
        return price_info

    def get_now_datetime(self):
        """
        获取当前时间
        """
        if self.mode in ["signal", "trade"]:
            # 回测时用回测的当前时间
            return self.datas.now_date
        return datetime.datetime.now()

    def get_opt_close_uids(self, code: str, mmd: str, allow_close_uids: list):
        # 实盘中起效果，允许执行的 close_uid 列表
        # 有多种格式
        #       列表格式：['a', 'b', 'c']，表示只在允许的 close_uid 中才允许操作
        #       字典格式：{'buy': ['a', 'b'], 'sell' : ['c', 'd']}，表示 buy 只在做多的仓位中允许，sell 只在做空的仓位中允许
        #       字典格式：{'1buy': ['a', 'b'], '1sell' : ['c', 'd']}，按照给定的买卖点，分别设置平仓信号
        #       字典格式：{'SHFE.RB': ['a', 'b', 'c'], 'SHFE.FU': {'buy': ['a'], 'sell': ['b']}} ,按照代码分别设置平仓信号
        if allow_close_uids is None:
            return ["clear"]

        # 列表格式的，直接返回
        if isinstance(allow_close_uids, list):
            return allow_close_uids

        # 按照买卖操作的类型，返回对应的 close_uid
        opt_type = "buy" if "buy" in mmd else "sell"
        if isinstance(allow_close_uids, dict) and opt_type in allow_close_uids.keys():
            return allow_close_uids[opt_type]

        # 按照买卖点的类型，返回对应的 close_uid
        if isinstance(allow_close_uids, dict) and mmd in allow_close_uids.keys():
            return allow_close_uids[mmd]

        # 按照代码分别设置的
        if isinstance(allow_close_uids, dict) and code in allow_close_uids.keys():
            return self.get_opt_close_uids(code, mmd, allow_close_uids[code])
        return ["clear"]

    def run(self, code, is_filter=False):
        """单代码单时间步的策略执行入口：先调用 close 检查平仓，再调用 open 寻找开仓机会。"""
        # begin_run_dt 用于跳过回测预热期，避免数据不足时产生错误信号
        if (
            self.begin_run_dt is not None
            and self.begin_run_dt >= self.get_now_datetime()
        ):
            return True

        # 先平仓再开仓，保证当日释放资金可被新仓位使用
        for _open_uid, pos in self.positions.items():
            if pos.code != code or pos.amount == 0:
                continue
            _time = time.time()
            opts = self.strategy.close(
                code=code, mmd=pos.mmd, pos=pos, market_data=self.datas
            )
            self.add_times("strategy_close", time.time() - _time)

            if opts is False or opts is None:
                continue
            if isinstance(opts, Operation):
                opts.code = code
                opts = [opts]
            for _opt in opts:
                _opt.code = code
                if self.mode != "signal":
                    allow_close_uids = self.get_opt_close_uids(
                        _opt.code, _opt.mmd, self.strategy.allow_close_uids
                    )
                    if _opt.close_uid not in allow_close_uids:
                        continue
                self.execute(code, _opt, pos)

        # 收集当前代码的有效持仓，传入 open() 以便策略感知当前仓位状态
        poss = [
            _p
            for _uid, _p in self.positions.items()
            if _p.code == code and _p.amount != 0
        ]

        _time = time.time()
        opts = self.strategy.open(code=code, market_data=self.datas, poss=poss)
        self.add_times("strategy_open", time.time() - _time)

        for opt in opts:
            opt.code = code
            if is_filter:
                # 过滤模式：暂存到缓冲区，由外部 filter_opts 二次筛选后再统一执行
                self.buffer_opts.append(opt)
            else:
                self.execute(code, opt, None)

        # 只保留有资金的持仓
        self.positions = {
            _uid: _p for _uid, _p in self.positions.items() if _p.amount != 0
        }

        return True

    def run_buffer_opts(self):
        """
        执行缓冲区的操作
        """
        for opt in self.buffer_opts:
            self.execute(opt.code, opt, None)
        self.buffer_opts = []

    def end(self):
        """回测结束时强制清仓所有持仓，避免未平仓头寸影响统计结果。"""
        for _uid, pos in self.positions.items():
            if pos.balance > 0:
                self.execute(
                    pos.code,
                    Operation(
                        opt="sell",
                        mmd=pos.mmd,
                        msg="退出",
                        code=pos.code,
                        close_uid="clear",
                    ),
                    pos,
                )
        return True

    def update_position_record(self):
        """
        更新所有持仓的盈亏情况
        """
        record_dt = self.get_now_datetime().strftime(self.record_dt_format)

        total_hold_profit = 0
        total_hold_balance = 0
        for _uid, pos in self.positions.items():
            if pos.amount == 0:
                continue
            now_profit, hold_balance = self.position_record(pos)
            total_hold_profit += now_profit
            total_hold_balance += hold_balance

        # 只有在交易模式下，才记录
        if self.mode != "trade":
            return True

        # 记录时间下的总持仓盈亏
        self.hold_profit_history[record_dt] = total_hold_profit

        # 记录当前的持仓金额
        position_balance = {}
        for _uid, pos in self.positions.items():
            if pos.amount == 0:
                continue
            code_price = self.get_price(pos.code)
            if "buy" in pos.mmd:
                code_balance = pos.amount * code_price["close"]
                if self.market == "futures":
                    code_balance = pos.balance - pos.release_balance
            else:
                code_balance = -(pos.amount * code_price["close"])
                if self.market == "futures":
                    code_balance = pos.balance - pos.release_balance
            if pos.code not in position_balance.keys():
                position_balance[pos.code] = 0
            position_balance[pos.code] += code_balance
        position_balance["Cash"] = self.balance

        self.balance_history[record_dt] = (
            total_hold_profit + total_hold_balance + self.balance
        )
        self.positions_balance_history[record_dt] = position_balance

        return None

    def position_record(self, pos: POSITION) -> Tuple[float, float]:
        """
        持仓记录更新
        :param pos:
        :return: 返回持仓的总金额（包括持仓盈亏）
        """
        s_time = time.time()

        hold_balance = pos.balance
        now_profit = 0
        price_info = self.get_price(pos.code)
        if pos.type == "做多":
            # 最大最小盈利百分比，改为单独是价格的百分比
            high_profit_rate = round(
                (price_info["high"] - pos.price) / pos.price * 100, 4
            )
            low_profit_rate = round(
                (price_info["low"] - pos.price) / pos.price * 100, 4
            )
            # 计算盈亏
            now_profit = (price_info["close"] - pos.price) * pos.amount

            if self.market == "futures":
                contract_info = self.futures_contracts[pos.code]
                high_profit_rate = round(
                    (price_info["high"] - pos.price)
                    / pos.price
                    / contract_info["margin_rate_long"]
                    * 100,
                    4,
                )
                low_profit_rate = round(
                    (price_info["low"] - pos.price)
                    / pos.price
                    / contract_info["margin_rate_long"]
                    * 100,
                    4,
                )
                now_profit = (
                    (price_info["close"] - pos.price)
                    * pos.amount
                    * contract_info["symbol_size"]
                )

            pos.max_profit_rate = max(pos.max_profit_rate, high_profit_rate)
            pos.max_loss_rate = min(pos.max_loss_rate, low_profit_rate)

        if pos.type == "做空":
            high_profit_rate = round(
                (pos.price - price_info["low"]) / pos.price * 100, 4
            )
            low_profit_rate = round(
                (pos.price - price_info["high"]) / pos.price * 100, 4
            )
            now_profit = (pos.price - price_info["close"]) * pos.amount

            if self.market == "futures":
                contract_info = self.futures_contracts[pos.code]
                high_profit_rate = round(
                    (pos.price - price_info["low"])
                    / pos.price
                    / contract_info["margin_rate_long"]
                    * 100,
                    4,
                )
                low_profit_rate = round(
                    (pos.price - price_info["high"])
                    / pos.price
                    / contract_info["margin_rate_long"]
                    * 100,
                    4,
                )
                now_profit = (
                    (pos.price - price_info["close"])
                    * pos.amount
                    * contract_info["symbol_size"]
                )

            pos.max_profit_rate = max(pos.max_profit_rate, high_profit_rate)
            pos.max_loss_rate = min(pos.max_loss_rate, low_profit_rate)

        self.add_times("position_record", time.time() - s_time)
        return now_profit, hold_balance

    def position_codes(self):
        """
        获取当前持仓中的股票代码
        """
        codes = list(
            set([_p.code for _uid, _p in self.positions.items() if _p.amount != 0])
        )
        return codes

    def hold_positions(self) -> List[POSITION]:
        """
        返回所有持仓记录
        """
        return [_p for _uid, _p in self.positions.items() if _p.amount != 0]

    def open_buy(self, code, opt: Operation, amount: float = None):
        """计算做多开仓的成交价格和数量；signal 模式用固定名义本金，trade 模式按实际可用资金分仓。"""
        if self.mode == "signal":
            # 信号模式，固定交易金额
            use_balance = 100000 * min(1.0, opt.pos_rate)
            price = self.get_price(code)["close"]
            amount = round((use_balance / price) * 0.99, 4)
            if self.market == "a":
                # 沪深买入数量是100的整数倍
                amount = int(amount / 100) * 100
            if self.market == "futures":
                # 如果是期货，按照期货的规则，计算可买的最大手数
                contract_config = self.futures_contracts[code]
                amount = int(
                    use_balance
                    / contract_config["margin_rate_long"]
                    / price
                    / contract_config["symbol_size"]
                )
            return {"price": price, "amount": amount}
        else:
            if len(self.hold_positions()) >= self.max_pos:
                return False
            price = self.get_price(code)["close"]

            use_balance = (
                self.balance / (self.max_pos - len(self.hold_positions()))
            ) * 0.99
            use_balance *= min(1.0, opt.pos_rate)
            # 避免资金成指数级别上升设置每笔最大占用资金
            if self.max_single_pos_balance is not None:
                use_balance = min(use_balance, self.max_single_pos_balance)
            amount = use_balance / price
            if self.market == "a":
                amount = int(amount / 100) * 100
            if self.market == "futures":
                contract_config = self.futures_contracts[code]
                amount = int(
                    use_balance
                    / contract_config["margin_rate_long"]
                    / price
                    / contract_config["symbol_size"]
                )

            if amount < 0:
                return False

            return {"price": price, "amount": amount}

    def open_sell(self, code, opt: Operation, amount: float = None):
        """计算做空开仓的成交价格和数量；逻辑与 open_buy 对称，使用空头保证金率。"""
        if self.mode == "signal":
            use_balance = 100000 * min(1.0, opt.pos_rate)
            price = self.get_price(code)["close"]
            amount = round((use_balance / price) * 0.99, 4)
            if self.market == "a":
                amount = int(amount / 100) * 100
            if self.market == "futures":
                # 如果是期货，按照期货的规则，计算可买的最大手数
                contract_config = self.futures_contracts[code]
                amount = int(
                    use_balance
                    / contract_config["margin_rate_short"]
                    / price
                    / contract_config["symbol_size"]
                )
            return {"price": price, "amount": amount}
        else:
            if len(self.hold_positions()) >= self.max_pos:
                return False
            price = self.get_price(code)["close"]

            use_balance = (
                self.balance / (self.max_pos - len(self.hold_positions()))
            ) * 0.99
            use_balance *= min(1.0, opt.pos_rate)
            # 避免资金成指数级别上升设置每笔最大占用资金
            if self.max_single_pos_balance is not None:
                use_balance = min(use_balance, self.max_single_pos_balance)
            amount = use_balance / price
            if self.market == "a":
                amount = int(amount / 100) * 100
            if self.market == "futures":
                contract_config = self.futures_contracts[code]
                amount = int(
                    use_balance
                    / contract_config["margin_rate_short"]
                    / price
                    / contract_config["symbol_size"]
                )

            if amount < 0:
                return False

            return {"price": price, "amount": amount}

    def close_buy(self, code, pos: POSITION, opt: Operation):
        """计算做多平仓的成交价格和数量；有止损价则按止损价成交，否则按当前收盘价。"""
        # 有止损价时按止损价成交（限价止损），否则以当前收盘价市价平仓
        if opt.loss_price != 0:
            price = opt.loss_price
        else:
            price = self.get_price(code)["close"]

        # 分仓平仓：按当前持仓比例折算本次应平数量
        # L1 防御性判零: execute 已有 now_pos_rate==0 前置守护, 此处兜底防直接调用 ZeroDivision
        if pos.now_pos_rate == 0:
            return {"price": price, "amount": 0}
        amount = pos.amount / pos.now_pos_rate * opt.pos_rate

        if self.mode == "signal":
            if self.market == "a":
                amount = int(amount / 100) * 100
            if self.market == "futures":
                amount = int(amount)
            return {"price": price, "amount": amount}
        else:
            # TODO 分仓场景下可能出现 amount=0 或平仓后有零头，待验证
            if self.market == "a":
                amount = int(amount / 100) * 100
            if self.market == "futures":
                amount = int(amount)
            return {"price": price, "amount": amount}

    def close_sell(self, code, pos: POSITION, opt: Operation):
        """计算做空平仓的成交价格和数量；逻辑与 close_buy 对称。"""
        if opt.loss_price != 0:
            price = opt.loss_price
        else:
            price = self.get_price(code)["close"]

        # L1 防御性判零: execute 已有 now_pos_rate==0 前置守护, 此处兜底防直接调用 ZeroDivision
        if pos.now_pos_rate == 0:
            return {"price": price, "amount": 0}
        amount = pos.amount / pos.now_pos_rate * opt.pos_rate

        if self.mode == "signal":
            if self.market == "a":
                amount = int(amount / 100) * 100
            if self.market == "futures":
                amount = int(amount)
            return {"price": price, "amount": amount}
        else:
            # TODO 分仓场景下可能出现 amount=0 或平仓后有零头，待验证
            if self.market == "a":
                amount = int(amount / 100) * 100
            if self.market == "futures":
                amount = int(amount)
            return {"price": price, "amount": amount}

    def cal_fee(
        self, code, price: float, balance: float, amount: float, other_info: dict = {}
    ):
        """计算单笔成交手续费；各市场规则不同，期货按合约配置，A 股按交易所标准。"""
        # 默认按成交额 × 手续费率计算（数字货币、港股、美股等通用）
        fee = balance * self.fee_rate

        if self.market == "futures":
            contract_config = self.futures_contracts[code]
            fee_rate = contract_config["fee_rate_open"]
            if "close" in other_info.keys() and other_info["close"]:
                fee_rate = contract_config["fee_rate_close"]
            if "close_today" in other_info.keys() and other_info["close_today"]:
                fee_rate = contract_config["fee_rate_close_today"]
            # fee_rate < 1：按成交额百分比；>= 1：每手固定金额
            if fee_rate == 0:
                fee = 0
            elif fee_rate < 1:
                fee = balance * fee_rate
            else:
                fee = fee_rate * amount

        if self.market == "a":
            # A 股费用构成：过户费 + 经手费 + 证管费 + 佣金（最低 5 元），卖出额外收印花税
            gh_fee_rate = 0.00001    # 过户费：0.01‰
            js_fee_rate = 0.0000341  # 经手费：0.0341‰
            zg_fee_rate = 0.00002    # 证管费：0.02‰
            yh_fee_rate = 0.0005     # 印花税：0.5‰，仅卖出收取

            fee = (
                (balance * gh_fee_rate)
                + (balance * js_fee_rate)
                + (balance * zg_fee_rate)
                + max((balance * self.fee_rate), 5)
            )
            if "sell" in other_info.keys() and other_info["sell"]:
                fee += balance * yh_fee_rate

        return fee

    def _print_log(self, msg):
        self.log_history.append(msg)
        if self.log:
            self.log(msg)
        return

    def _safe_alert(self, channel, title, msg, exc=None):
        """发告警, 通道失败时落地日志且不掩盖原异常 (M2)。

        告警通道 (飞书等) 自身网络异常时, 把失败落到本地日志, 避免静默漏单;
        若调用方传入原始异常 exc, 同样落地日志, 不被告警发送过程掩盖。
        """
        try:
            utils.send_fs_msg(channel, title, msg if isinstance(msg, list) else [msg])
        except Exception as alert_exc:
            # 告警通道失败必须落地日志, 否则静默漏单
            if self.log:
                self.log(f"[告警发送失败] {title}: {msg} | 通道异常: {alert_exc}")
        if exc is not None and self.log:
            self.log(f"[原始异常] {title}: {exc}")

    def execute(self, code, opt: Operation, pos: POSITION = None):
        """执行单笔开仓或平仓操作，更新持仓、资金和订单记录。"""
        _time = time.time()
        try:
            # 交易模式下由 allow_close_uids 统一控制平仓权限，close_uid 强制为 clear
            if self.mode != "signal":
                opt.close_uid = "clear"

            # pos 非空表示平仓请求；若持仓已清空则跳过，防止重复平仓
            if pos is not None:
                if pos.balance == 0.0 or pos.now_pos_rate == 0.0 or pos.amount == 0.0:
                    return True

            opt_mmd = opt.mmd
            # 检查是否在允许做的买卖点上
            if self.allow_mmds is not None and opt_mmd not in self.allow_mmds:
                return True

            if opt.opt == "buy":
                # 检查当前是否有该持仓，如果持仓存在的话，则不进行操作
                if (
                    opt.open_uid in self.positions.keys()
                    and self.positions[opt.open_uid].amount != 0
                ):
                    pos = self.positions[opt.open_uid]
                    if pos.now_pos_rate >= 1:
                        return True
                else:
                    pos = POSITION(code=code, mmd=opt.mmd, open_uid=opt.open_uid)
                    self.positions[opt.open_uid] = pos

            res = None
            order_type = None

            # 买点开多
            if "buy" in opt_mmd and opt.opt == "buy":
                # 同一位置（key）不重复开仓，防止信号重复触发
                if opt.key in pos.open_keys.keys():
                    return False
                # 修正开仓比例，确保累计仓位不超过 1.0
                opt.pos_rate = min(1.0 - pos.now_pos_rate, opt.pos_rate)

                res = self.open_buy(code, opt)
                if res is False:
                    return False

                pos.type = "做多"
                # BUG-1(Round8): 加仓按持仓量加权更新均价, 不覆盖为最后一笔成交价
                # (否则 position_record now_profit=(close-pos.price)*总amount 失真,
                #  污染 max_profit_rate/max_loss_rate → 移动止损/保本触发时机)。
                # 首笔 amount==0 退化为本笔成交价。做多/做空同口径(价按量加权)。
                if pos.amount == 0:
                    pos.price = res["price"]
                else:
                    pos.price = (
                        pos.price * pos.amount + res["price"] * res["amount"]
                    ) / (pos.amount + res["amount"])
                pos.amount += res["amount"]

                hold_balance = 0
                fee = 0
                if self.market == "futures":
                    # 期货保证金 = 成交价 × 手数 × 合约乘数 × 保证金率
                    contract_config = self.futures_contracts[code]
                    hold_balance = (
                        res["price"]
                        * res["amount"]
                        * contract_config["symbol_size"]
                        * contract_config["margin_rate_long"]
                    )
                    turnover_balance = (
                        res["price"] * res["amount"] * contract_config["symbol_size"]
                    )
                    fee = self.cal_fee(
                        code, res["price"], turnover_balance, res["amount"]
                    )
                else:
                    hold_balance = res["price"] * res["amount"]
                    fee = self.cal_fee(code, res["price"], hold_balance, res["amount"])

                pos.balance += hold_balance
                pos.fee += fee

                if self.mode == "trade":
                    self.balance -= hold_balance

                pos.loss_price = opt.loss_price
                # BUG-2(Round8): open_date 仅用于 T+1 判定, 取"最近一次建仓/加仓日期"。旧代码
                # 冻结首笔 -> 多日加仓后当日加仓的股当天可平 -> 绕过 T+1 高估收益。默认 pos_rate=1
                # 单笔满仓不加仓 -> 仍=首笔日, 零行为变更。
                pos.open_date = self.get_now_datetime().strftime("%Y-%m-%d")
                pos.open_datetime = (
                    self.get_now_datetime()
                    if pos.open_datetime is None
                    else pos.open_datetime
                )
                pos.open_msg = opt.msg
                pos.info = opt.info
                pos.now_pos_rate += min(1.0, opt.pos_rate)
                pos.open_keys[opt.key] = opt.pos_rate

                # 本次开仓的记录
                pos.open_records.append(
                    {
                        "datetime": self.get_now_datetime(),
                        "price": res["price"],
                        "amount": res["amount"],
                        "hold_balance": hold_balance,
                        "fee": fee,
                        "open_msg": opt.msg,
                        "open_key": opt.key,
                        "open_uid": opt.open_uid,
                        "pos_rate": opt.pos_rate,
                    }
                )

                order_type = "open_long"

                self._print_log(
                    f"[{code} - {self.get_now_datetime()}] // {opt_mmd} 做多买入（{res['price']} - {res['amount']}），原因： {opt.msg}"
                )

            # 卖点开空（仅期货/数字货币等支持做空的市场）
            if self.can_short and "sell" in opt_mmd and opt.opt == "buy":
                if opt.key in pos.open_keys.keys():
                    return False
                opt.pos_rate = min(1.0 - pos.now_pos_rate, opt.pos_rate)

                res = self.open_sell(code, opt)
                if res is False:
                    return False
                pos.type = "做空"
                # BUG-1(Round8): 加仓按持仓量加权更新均价, 不覆盖为最后一笔成交价
                # (否则 position_record now_profit=(close-pos.price)*总amount 失真,
                #  污染 max_profit_rate/max_loss_rate → 移动止损/保本触发时机)。
                # 首笔 amount==0 退化为本笔成交价。做多/做空同口径(价按量加权)。
                if pos.amount == 0:
                    pos.price = res["price"]
                else:
                    pos.price = (
                        pos.price * pos.amount + res["price"] * res["amount"]
                    ) / (pos.amount + res["amount"])
                pos.amount += res["amount"]

                hold_balance = 0
                fee = 0
                if self.market == "futures":
                    # 做空保证金 = 成交价 × 手数 × 合约乘数 × 空头保证金率
                    contract_config = self.futures_contracts[code]
                    hold_balance = (
                        res["price"]
                        * res["amount"]
                        * contract_config["symbol_size"]
                        * contract_config["margin_rate_short"]
                    )
                    turnover_balance = (
                        res["price"] * res["amount"] * contract_config["symbol_size"]
                    )
                    fee = self.cal_fee(
                        code, res["price"], turnover_balance, res["amount"]
                    )
                else:
                    hold_balance = res["price"] * res["amount"]
                    fee = self.cal_fee(code, res["price"], hold_balance, res["amount"])

                pos.balance += hold_balance
                pos.fee += fee

                if self.mode == "trade":
                    self.balance -= hold_balance

                pos.loss_price = opt.loss_price
                # BUG-2(Round8): open_date 仅用于 T+1 判定, 取"最近一次建仓/加仓日期"。旧代码
                # 冻结首笔 -> 多日加仓后当日加仓的股当天可平 -> 绕过 T+1 高估收益。默认 pos_rate=1
                # 单笔满仓不加仓 -> 仍=首笔日, 零行为变更。
                pos.open_date = self.get_now_datetime().strftime("%Y-%m-%d")
                pos.open_datetime = (
                    self.get_now_datetime()
                    if pos.open_datetime is None
                    else pos.open_datetime
                )
                pos.open_msg = opt.msg
                pos.info = opt.info
                pos.now_pos_rate += min(1.0, opt.pos_rate)
                pos.open_keys[opt.key] = opt.pos_rate

                pos.open_records.append(
                    {
                        "datetime": self.get_now_datetime(),
                        "price": res["price"],
                        "amount": res["amount"],
                        "hold_balance": hold_balance,
                        "fee": fee,
                        "open_msg": opt.msg,
                        "open_key": opt.key,
                        "open_uid": opt.open_uid,
                        "pos_rate": opt.pos_rate,
                    }
                )

                order_type = "open_short"

                self._print_log(
                    f"[{code} - {self.get_now_datetime()}] // {opt_mmd} 做空卖出（{res['price']} - {res['amount']}），原因： {opt.msg}"
                )

            # 卖点平空（期货做空平仓）
            if self.can_short and "sell" in opt_mmd and opt.opt == "sell":
                if opt.key in pos.close_keys.keys():
                    return False
                # close_uid 非 clear 时做幂等检查，防止同一信号重复平仓
                if opt.close_uid != "clear" and opt.close_uid in pos._close_uids:
                    return False
                # 修正平仓比例，不超过当前持仓比例
                opt.pos_rate = (
                    pos.now_pos_rate
                    if pos.now_pos_rate < opt.pos_rate
                    else opt.pos_rate
                )

                if (
                    self.can_close_today is False
                    and pos.open_date == self.get_now_datetime().strftime("%Y-%m-%d")
                ):
                    # A 股 T+1 规则：当日开仓不可当日平仓
                    return False

                res = self.close_sell(code, pos, opt)
                if res is False:
                    return False
                # Round11 N4: 实际成交量<=0(券商拒单/未成交, 如富途市价单被拒)视为平仓失败,
                # 不记录/不减仓, 防 clear 分支 now_pos_rate 归0而 amount 仍满 → execute 守卫永久跳过平仓(裸持失管)。
                if res.get("amount", 0) <= 0:
                    return False

                release_balance = 0
                fee = 0
                if self.market == "futures":
                    contract_config = self.futures_contracts[code]
                    # 释放保证金 = 平仓价 × 手数 × 合约乘数 × 空头保证金率
                    release_balance = (
                        res["price"]
                        * contract_config["symbol_size"]
                        * res["amount"]
                        * contract_config["margin_rate_short"]
                    )
                    turnover_balance = (
                        res["price"] * contract_config["symbol_size"] * res["amount"]
                    )
                    # 平今仓手续费率通常更高，需区分
                    fee_other = {"close": True}
                    if pos.open_date == fun.datetime_to_str(
                        self.get_now_datetime(), "%Y-%m-%d"
                    ):
                        fee_other = {"close_today": True}
                    fee = self.cal_fee(
                        code, res["price"], turnover_balance, res["amount"], fee_other
                    )
                else:
                    release_balance = res["price"] * res["amount"]
                    fee = self.cal_fee(
                        code, res["price"], release_balance, res["amount"]
                    )

                self._print_log(
                    "[%s - %s] // %s 平仓做空（%s - %s），原因： %s"
                    % (
                        code,
                        self.get_now_datetime(),
                        opt_mmd,
                        res["price"],
                        res["amount"],
                        opt.msg,
                    )
                )
                pos.close_records.append(
                    {
                        "datetime": self.get_now_datetime(),
                        "price": res["price"],
                        "amount": res["amount"],
                        "release_balance": release_balance,
                        "fee": fee,
                        "max_profit_rate": pos.max_profit_rate,
                        "max_loss_rate": pos.max_loss_rate,
                        "close_msg": opt.msg,
                        "close_key": opt.key,
                        "close_uid": opt.close_uid,
                        "pos_rate": opt.pos_rate,
                    }
                )
                pos._close_uids.add(opt.close_uid)

                # close_uid 非 clear 时只记录快照，不实际减仓；仅 clear 才执行真正平仓
                if opt.close_uid == "clear":
                    _full_shares = (
                        pos.amount / pos.now_pos_rate if pos.now_pos_rate > 0 else 0
                    )
                    pos.now_pos_rate -= opt.pos_rate
                    pos.close_keys[opt.key] = opt.pos_rate
                    pos.close_msg = opt.msg
                    pos.close_datetime = self.get_now_datetime()
                    pos.amount -= res["amount"]
                    # N4 部分成交兄弟: 券商部分成交(0<实际<意图量, 仅实盘真单可现; 回测/paper
                    # 恒全额→此兜底为死代码, 回测逐字节不变)致 amount 残留而 now_pos_rate 按完整
                    # 意图扣到0 → execute 入口守卫(now_pos_rate==0)永久跳过 → 残仓裸持失管。按残量
                    # 占满仓比例反推兜住 now_pos_rate 下限, 使下轮 execute 放行继续平残仓。
                    if pos.amount > 0 and pos.now_pos_rate <= 1e-9 and _full_shares > 0:
                        pos.now_pos_rate = pos.amount / _full_shares

                    pos.release_balance += release_balance
                    pos.fee += fee
                    if pos.amount == 0:
                        profit = 0
                        if self.market == "futures":
                            # 期货做空盈利 = (开仓保证金 - 平仓保证金) / 保证金率 - 手续费
                            contract_config = self.futures_contracts[code]
                            profit = (
                                pos.balance - pos.release_balance
                            ) / contract_config["margin_rate_short"] - pos.fee
                            profit_rate = profit / pos.balance * 100
                        else:
                            profit = pos.balance - pos.release_balance - pos.fee
                            profit_rate = profit / pos.balance * 100

                        pos.profit = profit
                        pos.profit_rate = profit_rate

                        self._print_log(
                            f"[{code} - {opt_mmd}] // 平仓做空 (开仓：{pos.open_datetime} / {pos.price} 平仓：{pos.close_datetime} / {res['price']}) (开仓资金：{pos.balance} 平仓资金：{pos.release_balance} 手续费：{pos.fee}) 盈亏：{profit} ({profit_rate:.2f}%)"
                        )

                        if pos.profit > 0:
                            self.results[opt_mmd]["win_num"] += 1
                            self.results[opt_mmd]["win_balance"] += pos.profit
                        else:
                            self.results[opt_mmd]["loss_num"] += 1
                            self.results[opt_mmd]["loss_balance"] += abs(pos.profit)

                        if self.mode == "trade":
                            self.balance += pos.balance + profit

                        if pos.code not in self.positions_history.keys():
                            self.positions_history[pos.code] = []
                        self.positions_history[pos.code].append(copy.deepcopy(pos))
                        self.fee_total += pos.fee

                order_type = "close_short"

            # 买点平多
            if "buy" in opt_mmd and opt.opt == "sell":
                if opt.key in pos.close_keys.keys():
                    return False
                if opt.close_uid != "clear" and opt.close_uid in pos._close_uids:
                    return False
                opt.pos_rate = (
                    pos.now_pos_rate
                    if pos.now_pos_rate < opt.pos_rate
                    else opt.pos_rate
                )

                if (
                    self.can_close_today is False
                    and pos.open_date == self.get_now_datetime().strftime("%Y-%m-%d")
                ):
                    # A 股 T+1 规则：当日开仓不可当日平仓
                    return False

                res = self.close_buy(code, pos, opt)
                if res is False:
                    return False
                # Round11 N4: 实际成交量<=0(券商拒单/未成交, 如富途市价单被拒)视为平仓失败,
                # 不记录/不减仓, 防 clear 分支 now_pos_rate 归0而 amount 仍满 → execute 守卫永久跳过平仓(裸持失管)。
                if res.get("amount", 0) <= 0:
                    return False

                release_balance = 0
                fee = 0
                if self.market == "futures":
                    contract_config = self.futures_contracts[code]
                    # 释放保证金 = 平仓价 × 手数 × 合约乘数 × 多头保证金率
                    release_balance = (
                        res["price"]
                        * contract_config["symbol_size"]
                        * res["amount"]
                        * contract_config["margin_rate_long"]
                    )
                    turnover_balance = (
                        res["price"] * contract_config["symbol_size"] * res["amount"]
                    )
                    # 平今仓手续费率通常更高，需区分
                    fee_other = {"close": True}
                    if pos.open_date == fun.datetime_to_str(
                        self.get_now_datetime(), "%Y-%m-%d"
                    ):
                        fee_other = {"close_today": True}
                    fee = self.cal_fee(
                        code, res["price"], turnover_balance, res["amount"], fee_other
                    )
                else:
                    release_balance = res["price"] * res["amount"]
                    # A 股卖出需传 sell=True 以触发印花税计算
                    fee = self.cal_fee(
                        code,
                        res["price"],
                        release_balance,
                        res["amount"],
                        other_info={"sell": True},
                    )

                self._print_log(
                    "[%s - %s] // %s 平仓做多（%s - %s），原因： %s"
                    % (
                        code,
                        self.get_now_datetime(),
                        opt_mmd,
                        res["price"],
                        res["amount"],
                        opt.msg,
                    )
                )
                # 本次平仓的记录
                pos.close_records.append(
                    {
                        "datetime": self.get_now_datetime(),
                        "price": res["price"],
                        "amount": res["amount"],
                        "release_balance": release_balance,
                        "fee": fee,
                        "max_profit_rate": pos.max_profit_rate,
                        "max_loss_rate": pos.max_loss_rate,
                        "close_msg": opt.msg,
                        "close_key": opt.key,
                        "close_uid": opt.close_uid,
                        "pos_rate": opt.pos_rate,
                    }
                )
                pos._close_uids.add(opt.close_uid)

                # close_uid 非 clear 时只记录快照，不实际减仓；仅 clear 才执行真正平仓
                if opt.close_uid == "clear":
                    _full_shares = (
                        pos.amount / pos.now_pos_rate if pos.now_pos_rate > 0 else 0
                    )
                    pos.now_pos_rate -= opt.pos_rate
                    pos.close_keys[opt.key] = opt.pos_rate
                    pos.close_msg = opt.msg
                    pos.close_datetime = self.get_now_datetime()
                    pos.amount -= res["amount"]
                    # N4 部分成交兄弟: 券商部分成交(0<实际<意图量, 仅实盘真单可现; 回测/paper
                    # 恒全额→此兜底为死代码, 回测逐字节不变)致 amount 残留而 now_pos_rate 按完整
                    # 意图扣到0 → execute 入口守卫(now_pos_rate==0)永久跳过 → 残仓裸持失管。按残量
                    # 占满仓比例反推兜住 now_pos_rate 下限, 使下轮 execute 放行继续平残仓。
                    if pos.amount > 0 and pos.now_pos_rate <= 1e-9 and _full_shares > 0:
                        pos.now_pos_rate = pos.amount / _full_shares

                    pos.release_balance += release_balance
                    pos.fee += fee
                    if pos.amount == 0:
                        profit = 0
                        if self.market == "futures":
                            # 期货做多盈利 = (平仓保证金 - 开仓保证金) / 保证金率 - 手续费
                            contract_config = self.futures_contracts[code]
                            profit = (
                                pos.release_balance - pos.balance
                            ) / contract_config["margin_rate_long"] - pos.fee
                            profit_rate = profit / pos.balance * 100
                        else:
                            profit = pos.release_balance - pos.balance - pos.fee
                            profit_rate = profit / pos.balance * 100

                        pos.profit = profit
                        pos.profit_rate = profit_rate

                        self._print_log(
                            f"[{code} - {opt_mmd}] // 平仓做多 (开仓：{pos.open_datetime} / {pos.price} 平仓：{pos.close_datetime} / {res['price']}) (开仓资金：{pos.balance} 平仓资金：{pos.release_balance} 手续费：{pos.fee}) 盈亏：{profit} ({profit_rate:.2f}%)"
                        )

                        if pos.profit > 0:
                            self.results[opt_mmd]["win_num"] += 1
                            self.results[opt_mmd]["win_balance"] += pos.profit
                        else:
                            self.results[opt_mmd]["loss_num"] += 1
                            self.results[opt_mmd]["loss_balance"] += abs(pos.profit)

                        if self.mode == "trade":
                            self.balance += pos.balance + profit

                        if pos.code not in self.positions_history.keys():
                            self.positions_history[pos.code] = []
                        self.positions_history[pos.code].append(copy.deepcopy(pos))
                        self.fee_total += pos.fee

                order_type = "close_long"

            if res:
                # 记录订单信息
                if code not in self.orders:
                    self.orders[code] = []
                self.orders[code].append(
                    {
                        "datetime": self.get_now_datetime(),
                        "type": order_type,
                        "price": res["price"],
                        "amount": res["amount"],
                        "info": opt.msg,
                        "open_uid": opt.open_uid,
                        "close_uid": opt.close_uid,
                    }
                )
                return True

            return False
        finally:
            self.add_times("execute", time.time() - _time)

    def order_draw_tv_mark(
        self,
        market: str,
        mark_label: str,
        close_uid: List[str] = None,
        start_dt: datetime = None,
    ):
        # 先删除所有的订单
        db.marks_del(market=market, mark_label=mark_label)
        order_colors = {
            "open_long": "red",
            "open_short": "green",
            "close_long": "green",
            "close_short": "red",
        }
        order_shape = {
            "open_long": "earningUp",
            "open_short": "earningDown",
            "close_long": "earningDown",
            "close_short": "earningUp",
        }
        for _code, _orders in self.orders.items():
            for _o in _orders:
                if close_uid is not None:
                    if "close_" in _o["type"] and _o["close_uid"] not in close_uid:
                        continue
                if start_dt is not None:
                    if fun.datetime_to_int(_o["datetime"]) < fun.datetime_to_int(
                        start_dt
                    ):
                        continue
                db.marks_add(
                    market,
                    _code,
                    _code,
                    "",
                    fun.datetime_to_int(_o["datetime"]),
                    mark_label,
                    _o["info"],
                    order_shape[_o["type"]],
                    order_colors[_o["type"]],
                )
        print("Done")
        return True
