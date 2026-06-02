# -*- coding: utf-8 -*-
import datetime
from typing import Union, List, Tuple, Any, Optional
import pandas as pd

from chanlun.core.bi_calculator import BiCalculator
# 笔/段两层中枢统一走 ZsCalculator，create_dn_zs 也用临时 ZsCalculator 实例。
from chanlun.core.cl_interface import ICL, Kline, CLKline, FX, BI, XD, ZS, ZSLX, Config, LINE, query_macd_ld
from chanlun.core.zslx_calculator import ZslxCalculator
from chanlun.core import beichi_calculator as bc
from chanlun.core.cl_kline_process import CL_Kline_Process
from chanlun.core.kline_data_processor import KlineDataProcessor
from chanlun.core.macd import MACD
from chanlun.core.macd_htf import compute_higher_macd
from chanlun.core.xd_calculator import XdCalculator
from chanlun.core.zs_calculator import ZsCalculator
from chanlun.core.recursive_calculator import RecursiveCalculator, LevelResult
from chanlun.core.interval_nest_calculator import calculate_interval_nest, IntervalNest
from chanlun.tools.log_util import LogUtil


class CL(ICL):
    """
    缠论分析主类
    实现缠论的完整分析流程，包括K线处理、分型识别、笔线段计算、中枢分析等
    """

    def __init__(
            self,
            code: str,
            frequency: str,
            config: Union[dict, None] = None,
            start_datetime: datetime.datetime = None,
            market: Union[str, None] = None,
    ):
        """
        初始化缠论分析器

        Args:
            code: 标的代码
            frequency: 分析周期
            config: 配置参数字典
            start_datetime: 开始分析时间
            market: 市场标识（可选）。仅高周期 MACD 的日级及以上分桶用于时区
                偏移；1m→5m、5m→30m 纯按时间戳重采样，不依赖此值。
        """
        self.code = code
        self.frequency = frequency
        self.config = config if config else {}
        self.start_datetime = start_datetime
        self.market = market

        # 设置默认配置
        self._init_default_config()

        # 实例化K线数据处理器
        self.kline_processor = KlineDataProcessor(self.start_datetime)
        # 实例化缠论K线处理器，用于处理包含关系
        self.cl_kline_processor = CL_Kline_Process()
        # 实例化MACD计算器
        self.macd_calculator = MACD()
        # 高周期 MACD（背驰力度判断用）。process_klines 每轮重算；
        # None 表示不可用（开关关 / 无高周期 / 桶不足），query_macd_ld 回退原生。
        self._htf_macd: Union[dict, None] = None
        # 实例化笔计算器
        self.bi_calculator = BiCalculator(bi_mode = 'strict')
        # 实例化线段计算器
        self.xd_calculator = XdCalculator(self.config)

        self.zss_calculator = ZsCalculator()
        # 笔层中枢计算器，与 zss_calculator（线段层）独立维护。
        self.bi_zss_calculator = ZsCalculator()

        # 走势类型计算器（子项目③）：线段中枢 → 走势类型(ZSLX)，无状态全量重算。
        self.zslx_calculator = ZslxCalculator()
        self.xd_zslx: List[ZSLX] = []  # 线段级走势类型列表

        # 最后中枢缓存
        self._last_bi_zs: Union[ZS, None] = None
        self._last_xd_zs: Union[ZS, None] = None

        # process_mmd 触发签名缓存：仅当 xds + bis 尾部签名（长度/末段 K 索引/末段
        # done 状态）变化才重跑，避免每根 K 线全量扫描 1B/2B/3B（O(N²)）。
        # 签名同时覆盖 xd / bi 尾部，任一层变化都需重跑。
        self._last_mmd_sig: Union[tuple, None] = None

        # 兼容运行时期望字段
        self.debug: bool = False
        self.use_time: dict = {}

    def _init_default_config(self):
        """初始化默认配置参数"""
        default_config = {
            # 运行时兼容标识
            'config_use_type': 'common',
            # K线类型配置
            'kline_type': Config.KLINE_TYPE_DEFAULT.value,
            'kline_qk': Config.KLINE_QK_NONE.value,

            # 分型配置
            'fx_qy': Config.FX_QY_THREE.value,
            'fx_qj': Config.FX_QJ_CK.value,
            'fx_bh': Config.FX_BH_NO.value,

            # 笔配置
            'bi_type': Config.BI_TYPE_NEW.value,
            'bi_bzh': Config.BI_BZH_YES.value,
            'bi_qj': Config.BI_QJ_DD.value,
            'bi_fx_cgd': Config.BI_FX_CHD_NO.value,

            # 线段配置
            'xd_qj': Config.XD_QJ_DD.value,
            'xd_bzh': Config.XD_BZH_YES.value,
            'xd_bi_pohuai': Config.XD_BI_POHUAI_NO.value,

            # 中枢配置
            'zs_type_bi': Config.ZS_TYPE_BZ.value,
            'zs_type_xd': Config.ZS_TYPE_BZ.value,
            # 老下游通过 cd.get_config()["zs_bi_type"] 拿笔中枢类型列表，
            # 给等价于 [zs_type_bi] 的默认值保持向后兼容，避免 KeyError。
            'zs_bi_type': [Config.ZS_TYPE_BZ.value],
            'zs_xd_type': [Config.ZS_TYPE_BZ.value],
            'zs_qj': Config.ZS_QJ_DD.value,
            'zs_cd': Config.ZS_CD_THREE.value,
            # 中枢位置关系 / 趋势判定档(§1.4 + §3.7):
            #   - GD (默认): 原文严格档——趋势中前后中枢「绝对不存在重叠,包括
            #     围绕中枢的瞬间波动(GG/DD)之间的重叠」(原文行2891/3565/3566/
            #     7795)。gg/dd 包络无交集才算趋势;ZG/ZD 分离但 GG/DD 重叠 = 中枢
            #     扩展(高级别中枢)、非趋势。按原文重做,默认取此严格档。
            #   - ZGD: 仅比中枢核心区间 zd/zg(后 zd>前 zg 即趋势),宽于原文——
            #     会把原文判为「中枢扩展」的情形误判为趋势,实务信号更多。
            #   - ZGGDD: 兼容老用法,zg 与 dd、zd 与 gg 比较,介于两者之间。
            # 注:本配置同时驱动 buy/sell 点链路 (cl.beichi_qs / zss_is_qs)
            # 与递归链路 (③ ZSLX 划分 / ④ recursive / ⑤ interval_nest),
            # 见 process_klines / get_recursive_levels / get_interval_nest。
            'zs_wzgx': Config.ZS_WZGX_GD.value,
            'cal_last_zs': True,
            'use_macd_ld': True,
            # 背驰力度判断用高一周期 MACD（线段是最低级别走势类型，度量其
            # 力度应提高一个级别：1m→5m、5m→30m…）。开关关 / 无高周期 /
            # 高周期桶不足时 query_macd_ld 自动回退原生 MACD。
            'macd_ld_use_htf': True,
        }

        for key, value in default_config.items():
            if key not in self.config:
                self.config[key] = value

    def process_klines(self, klines: pd.DataFrame):
        """
        处理K线数据，计算缠论分析结果
        支持增量更新：通过对比K线数量和最后一根K线状态，避免不必要的重复计算。

        内部流水线直接引用子计算器数据避免 deepcopy 开销；外部 get_xxx() 仍会
        deepcopy 保证安全。流水线任一环节抛异常时在 except 里重置签名缓存与
        中枢/MMD 状态，避免「half-applied」状态被下次调用按"未变化"路径跳过。
        """
        # 返回增量更新或新增的K线数据列表
        src_klines: List[Kline] = self.kline_processor.process_kline(klines)
        if not src_klines:
            return self

        try:
            # 直接引用内部数据，避免 deepcopy
            # 使用MACD计算器更新指标
            self.macd_calculator.process_macd(self.kline_processor.klines)
            # 高周期 MACD：背驰力度按原文应在高一级别上度量。每轮全量重算。
            self._compute_htf_macd()

            # 更新缠论K线：process_cl_klines 是内部状态更新器，不依赖返回值。
            # 下游直接继续消费 self.cl_kline_processor.cl_klines。
            self.cl_kline_processor.process_cl_klines(self.kline_processor.klines)

            # 计算笔和分型 - 直接引用 cl_klines
            self.bi_calculator.calculate(self.cl_kline_processor.cl_klines)

            # 计算线段 - 直接引用 bis
            self.xd_calculator.calculate(self.bi_calculator.bis)

            # 计算中枢 - 直接引用 xds
            self.zss_calculator.calculate(self.xd_calculator.xds)

            # 笔层中枢与线段层对称接入，主流程自动算。两个 ZsCalculator 实例
            # 状态/快照互不污染；任一异常由外层 except 统一清理。
            self.bi_zss_calculator.calculate(self.bi_calculator.bis)

            # 走势类型划分（子项目③）：用线段中枢 + 背驰划出走势类型，并回填
            # 线段中枢的 zs.type（上涨/下跌中枢 up/down、盘整中枢 zd）。
            xd_zss = list(self.zss_calculator.zss)
            if self.zss_calculator.pending_zs is not None:
                xd_zss.append(self.zss_calculator.pending_zs)
            ld_provider = lambda s, e: query_macd_ld(self, s, e)
            # 递归链路（③走势类型 / ④递归 / ⑤区间套）默认走原文严格档 GD：
            # 趋势判定须按走势中枢中心定理二的 GG/DD 口径（原文「后GG<前DD
            # → 下跌、后DD>前GG → 上涨」）。默认 config.zs_wzgx 在 §1.4 后即
            # 为 GD,递归链路、买卖点链路统一读 config(§3.7),保证用户显式
            # opt-out 到 ZGGDD 等较宽档时所有链路口径一致,避免「买卖点说趋
            # 势 / ZSLX 说盘整」的双口径冲突。
            recursive_wzgx = self.config.get('zs_wzgx', Config.ZS_WZGX_GD.value)
            self.xd_zslx = self.zslx_calculator.calculate(
                xd_zss, self.xd_calculator.xds, ld_provider, recursive_wzgx,
                self.frequency,
            )

            # 笔中枢方向回填(§3.6,原 ① 路线图遗留)：用同一套 ③ 走势类型划分
            # 给笔层中枢回填方向(上涨/下跌中枢 up/down、盘整中枢 zd)。下游
            # cl_analyse 等用 zs.type 做 up/down 二分逻辑时,笔中枢拿到正确
            # 方向(此前是 ZsCalculator 内部置的 seg_b.type 占位、与原文不符)。
            # 仅用副作用(回填),不存储 bi_zslx——bi 层走势类型尚无消费方,YAGNI。
            bi_zss = list(self.bi_zss_calculator.zss)
            if self.bi_zss_calculator.pending_zs is not None:
                bi_zss.append(self.bi_zss_calculator.pending_zs)
            if bi_zss:
                self.zslx_calculator.calculate(
                    bi_zss, self.bi_calculator.bis, ld_provider, recursive_wzgx,
                    self.frequency,
                )

            # 每次处理后重置缓存，确保下次访问时重新计算
            self._last_bi_zs = None
            self._last_xd_zs = None

            # 自动连带跑 process_mmd，避免 web 路径漏调用导致前端看不到买卖点。
            # process_mmd 内部已做去重，增量调用幂等。
            # 用 xds + bis 尾部签名做脏检查：BsPointCalculator.calculate 内部是 3 个
            # O(N) 全量扫描，尾部无「新增/端点变化/done 翻转」时复用上一轮结果。
            new_sig = (
                self._calc_layer_sig(self.xd_calculator.xds),
                self._calc_layer_sig(self.bi_calculator.bis),
            )
            if new_sig != self._last_mmd_sig:
                self.process_mmd()
                self._last_mmd_sig = new_sig
        except Exception:
            # 任意子步骤失败 → 清空外层签名 / 中枢缓存，强制下次走全量分支，
            # 避免内部 calculator 留下的脏 snapshot 影响下次结果。
            self._last_mmd_sig = None
            self._last_bi_zs = None
            self._last_xd_zs = None
            # 走势类型随中枢缓存一并清空：否则异常后 xd_zslx 残留旧走势类型，
            # 其 zss 引用的可能是已失效的中枢实例。
            self.xd_zslx = []
            # 同时清掉两个 ZsCalculator 的内部 snapshot：否则子计算器的
            # _last_lines_count / _last_tail_snapshot 可能停在「半截状态」，
            # 下次会被当普通增量从错误 entry_idx 续算 → 漏识别中枢。
            try:
                self.zss_calculator._last_lines_count = 0
                self.zss_calculator._last_tail_snapshot = None
                self.bi_zss_calculator._last_lines_count = 0
                self.bi_zss_calculator._last_tail_snapshot = None
            except AttributeError:
                # 兼容老版本 ZsCalculator（没有这两个字段）
                pass
            raise

        return self

    # --- ICL 接口实现 ---
    def get_code(self) -> str:
        """返回标的代码"""
        return self.code

    def get_frequency(self) -> str:
        """返回分析周期"""
        return self.frequency

    def get_config(self) -> dict:
        """返回配置参数"""
        return self.config

    def get_src_klines(self) -> List[Kline]:
        """返回原始K线列表（浅拷贝，防止外部修改列表结构）"""
        return list(self.kline_processor.klines)

    def get_klines(self) -> List[Any]:
        """返回K线列表"""
        if self.config.get('kline_type') == Config.KLINE_TYPE_CHANLUN.value:
            return self.get_cl_klines()
        else:
            return self.get_src_klines()

    def get_cl_klines(self) -> List[CLKline]:
        """返回缠论K线列表（浅拷贝）"""
        return list(self.cl_kline_processor.cl_klines)

    def get_idx(self) -> dict:
        """返回技术指标数据"""
        # 从MACD计算器获取结果
        return self.macd_calculator.get_results()

    def _compute_htf_macd(self) -> None:
        """计算高周期 MACD，写入 ``self._htf_macd``，供 query_macd_ld 取用。

        背驰力度按原文应在「高一级别」上度量（本项目以线段为最低级别走势
        类型）。开关 ``macd_ld_use_htf`` 关、无高周期对照、或高周期 K 线桶
        不足时置 None —— query_macd_ld 据此回退原生 MACD。
        """
        flag = self.config.get('macd_ld_use_htf', True)
        # 兼容 bool / 字符串("0"/"1") 两种配置写法
        if flag in (False, 0, '0', 'false', 'False', ''):
            self._htf_macd = None
            return
        self._htf_macd = compute_higher_macd(
            self.kline_processor.klines,
            self.frequency,
            self.market,
            fast=int(self.config.get('idx_macd_fast', 12)),
            slow=int(self.config.get('idx_macd_slow', 26)),
            signal=int(self.config.get('idx_macd_signal', 9)),
        )

    def get_fxs(self) -> List[FX]:
        """返回分型列表（浅拷贝）"""
        return list(self.bi_calculator.fxs)

    def get_bis(self) -> List[BI]:
        """返回笔列表（浅拷贝）"""
        return list(self.bi_calculator.bis)

    def get_xds(self) -> List[XD]:
        """返回线段列表（浅拷贝）"""
        return list(self.xd_calculator.xds)

    def get_bi_zss(self, zs_type: str = None) -> List[ZS]:
        """返回笔中枢列表

        从 bi_zss_calculator 读取最新状态，与 get_xd_zss 对称（已完成 zss +
        当前 pending_zs）。zs_type 当前只有 BZ 一种走法，保留仅为接口兼容。
        """
        zss = list(self.bi_zss_calculator.zss)
        if self.bi_zss_calculator.pending_zs is not None:
            zss.append(self.bi_zss_calculator.pending_zs)
        return zss

    def get_xd_zss(self, zs_type: str = None) -> List[ZS]:
        """返回线段中枢字典"""
        zss = list(self.zss_calculator.zss)
        if self.zss_calculator.pending_zs:
            zss.append(self.zss_calculator.pending_zs)
        return zss

    def get_xd_zslx(self) -> List[ZSLX]:
        """返回线段级走势类型列表（子项目③产出）。"""
        return self.xd_zslx

    def get_recursive_levels(self) -> List[LevelResult]:
        """返回递归装配的多级层级树（子项目④）。

        并存独立子系统：每次现算（层级单元数逐级收缩、开销可忽略），
        不接入 process_klines、不动周期多级分析与 bs_point_calculator。
        """
        ld_provider = lambda s, e: query_macd_ld(self, s, e)
        # 递归链路默认原文严格档 GD,允许 config opt-out(§3.7,统一各链路读
        # config)——理由见 process_klines ③ 接入处注释。
        recursive_wzgx = self.config.get('zs_wzgx', Config.ZS_WZGX_GD.value)
        return RecursiveCalculator().calculate(
            self.xd_calculator.xds, ld_provider, recursive_wzgx, self.frequency,
        )

    def get_recursive_mmds(self) -> dict:
        """在递归各「升级」级别上识别买卖点（中枢升级买卖点）。

        并存独立子系统：每次现算，结果存入各级走势单元的 ``zs_type_mmds['L{level}']``
        独立分桶，**不与 bi/xd 基础买卖点冲突**。返回 ``{level: [(name, k_index, val)…]}``。

        原文依据：中枢升级后，升级级别中枢自身的一二三类买卖点（第54课「升级三卖」等）
        是更高级别的转折信号。注：升级是否出现取决于数据是否具备更高级别结构——
        低结构标的（整段一个大盘整/扩展）可能升不出级别，属原文-一致。
        """
        from chanlun.core.bs_point_calculator import BsPointCalculator
        levels = self.get_recursive_levels()
        if not levels:
            return {}
        result: dict = {}
        xds = list(self.xd_calculator.xds)
        for lv in levels:
            units = xds if lv.level == 0 else levels[lv.level - 1].zslxs
            zt = f'L{lv.level}'
            BsPointCalculator(self, zs_type=zt).calculate(units, lv.zss)
            pts = []
            for u in units:
                for m in u.zs_type_mmds.get(zt, []):
                    try:
                        pts.append((m.name, u.end.k.k_index, round(u.end.val, 3)))
                    except Exception:
                        pass
            result[lv.level] = pts
        return result

    def get_interval_nest(self) -> Optional[IntervalNest]:
        """返回「当下」区间套——从最高级别趋势背驰逐级下钻定位转折点（区间套）。

        并存独立子系统：每次现算，不接入 process_klines、不动下游。
        """
        ld_provider = lambda s, e: query_macd_ld(self, s, e)
        # 递归链路默认原文严格档 GD,允许 config opt-out(§3.7,统一各链路读
        # config)——理由见 process_klines ③ 接入处注释。
        recursive_wzgx = self.config.get('zs_wzgx', Config.ZS_WZGX_GD.value)
        return calculate_interval_nest(
            self.get_recursive_levels(),
            self.xd_calculator.xds,
            ld_provider,
            recursive_wzgx,
            self.frequency,
        )

    # ===== 新核心 8 模块 lazy 接入(并存,不动旧版/process_klines/golden) =====
    def get_recursive_branch_levels(self):
        """新核心:recursive_branch 多级递归(中枢+内联背驰+走势类型,P1-P4)。

        与旧 get_recursive_levels(RecursiveCalculator)并存独立——本方法走重做的
        zs_branch/zslx_branch/recursive_branch 链路,默认 ZGD(合原文「≥2 依次同向中枢」)。
        lazy 现算、不接 process_klines。返回 List[recursive_branch.LevelResult]。
        """
        from chanlun.core.recursive_branch import RecursiveBranchCalculator
        ld = lambda s, e: query_macd_ld(self, s, e)
        wzgx = self.config.get('zs_wzgx', Config.ZS_WZGX_ZGD.value)
        # L0 输入用线段(xds):线段中枢是缠论最低正式级别中枢(宪法 L0=线段),升级链由此起。
        # 笔中枢是更小的观察级别、不参与升级,单独走 get_bi_zhongshu。
        # (1m 标的线段稀疏→线段中枢少,但缠论正确;丰富的笔中枢另在观察层显示。)
        return RecursiveBranchCalculator().calculate(
            list(self.get_xds()), ld, wzgx, self.frequency,
        )

    def get_bi_zhongshu(self):
        """笔中枢:新核心 zs_branch 直接在笔(bis)上找的中枢。

        缠论里笔中枢是比线段中枢更小的「观察级别」,不参与升级链(升级从线段中枢起,
        见 get_recursive_branch_levels L0=线段)。返回 List[ZS]。
        """
        from chanlun.core.zs_branch import ZsBranchCalculator
        ld = lambda s, e: query_macd_ld(self, s, e)
        wzgx = self.config.get('zs_wzgx', Config.ZS_WZGX_ZGD.value)
        res = ZsBranchCalculator(
            ld_provider=ld, frequency=self.frequency, wzgx=wzgx, min_zs_lines=4,
        ).calculate(list(self.get_bis()))
        return res.done_zss

    def get_branch_bspoints(self):
        """新核心:一/二/三类买卖点(全多级,P5a-d)。lazy 并存。返回 List[BuySellPoint]。

        L0 一三类(bs_branch 单级)+ 二类(bs2 跨级)+ L1+ 扩张三买(bs3,过滤 L0 去重)。
        一类多级(L1+ 背驰一类)留后。
        """
        from chanlun.core.recursive_branch import RecursiveBranchCalculator
        from chanlun.core.zs_branch import ZsBranchResult
        from chanlun.core.bs_branch import BsBranchCalculator
        from chanlun.core.bs2_branch import Bs2BranchCalculator
        from chanlun.core.bs3_branch import Bs3BranchCalculator
        # 买卖点走笔级递归(细粒度操作信号,保持用户习惯);中枢显示的线段级见
        # get_recursive_branch_levels。线段中枢稀疏→线段级买卖点很少,笔级更实用。
        ld = lambda s, e: query_macd_ld(self, s, e)
        wzgx = self.config.get('zs_wzgx', Config.ZS_WZGX_ZGD.value)
        levels = RecursiveBranchCalculator().calculate(
            list(self.get_bis()), ld, wzgx, self.frequency,
        )
        if not levels:
            return []
        l0 = levels[0]
        zr0 = ZsBranchResult(done_zss=l0.zss, live=[], freeze_idx=0,
                             done_divergence=l0.done_divergence)
        pts = list(BsBranchCalculator().calculate(zr0, l0.units))          # L0 一三类
        pts += Bs2BranchCalculator().calculate(levels)                     # 二类(跨级)
        pts += [p for p in Bs3BranchCalculator().calculate(levels)
                if p.level is not None and p.level >= 1]                   # L1+ 扩张三买(不重 L0)
        return pts

    def get_branch_interval_nest(self):
        """新核心:区间套可操作性(P6,自顶向下 READ)。lazy 并存。返回 List[NestRead]。"""
        from chanlun.core.beichi_nest import BeichiNestCalculator
        from chanlun.core.interval_nest import IntervalNestCalculator
        forest = BeichiNestCalculator().calculate(self.get_recursive_branch_levels())
        return IntervalNestCalculator().calculate(forest)

    def get_last_bi_zs(self) -> Union[ZS, None]:
        """返回最后的笔中枢

        直接读 bi_zss_calculator 最新状态，与 get_bi_zss() 尾部一致：优先取
        pending_zs（最新未完成），退而取 zss[-1]。不用截尾重算的老套路——
        数据范围变少会让进入段定位偏移。
        """
        if not self.config.get('cal_last_zs', True):
            return None

        if self._last_bi_zs is None:
            if self.bi_zss_calculator.pending_zs is not None:
                self._last_bi_zs = self.bi_zss_calculator.pending_zs
            elif self.bi_zss_calculator.zss:
                self._last_bi_zs = self.bi_zss_calculator.zss[-1]

        return self._last_bi_zs

    def get_last_xd_zs(self) -> Union[ZS, None]:
        """
        返回最后的线段中枢。

        直接复用 zss_calculator 状态，与 get_xd_zss() 尾部一致：优先取
        pending_zs（最近未完成），退而取 zss[-1]。不在主流程外重算，
        否则数据范围变少会让进入段定位偏移、结果口径不一致。
        """
        if not self.config.get('cal_last_zs', True):
            return None

        if self._last_xd_zs is None:
            # 优先 pending（最新但未完成），其次取最后一个完成的
            if self.zss_calculator.pending_zs is not None:
                self._last_xd_zs = self.zss_calculator.pending_zs
            elif self.zss_calculator.zss:
                self._last_xd_zs = self.zss_calculator.zss[-1]

        return self._last_xd_zs

    def beichi_pz(self, zs: ZS, now_line: LINE) -> Tuple[bool, Union[LINE, None]]:
        """判断中枢与指定线是否构成盘整背驰（薄壳，逻辑见 beichi_calculator）。"""
        ld_provider = lambda s, e: query_macd_ld(self, s, e)
        return bc.beichi_pz(zs, now_line, ld_provider, self.frequency)

    def beichi_qs(
            self, lines: List[LINE], zss: List[ZS], now_line: LINE
    ) -> Tuple[bool, List[LINE]]:
        """判断指定线与之前的中枢是否形成趋势背驰（薄壳，逻辑见 beichi_calculator）。"""
        ld_provider = lambda s, e: query_macd_ld(self, s, e)
        wzgx_config = self.config.get('zs_wzgx', Config.ZS_WZGX_GD.value)
        # 买卖点链路用「中枢本体包络」判趋势(剔除波动/延伸/离开段)——否则远摆撑爆
        # 包络、趋势恒判不出、1类不触发(见 combination_calculator.core_envelope)。
        return bc.beichi_qs(
            lines, zss, now_line, ld_provider, wzgx_config, self.frequency,
            use_core_envelope=True,
        )

    def zss_is_qs(self, one_zs: ZS, two_zs: ZS) -> Union[str, None]:
        """判断两个中枢是否形成趋势（薄壳，逻辑见 beichi_calculator）。"""
        wzgx_config = self.config.get('zs_wzgx', Config.ZS_WZGX_GD.value)
        return bc.is_qs(one_zs, two_zs, wzgx_config, use_core_envelope=True)

    def create_dn_zs(
        self,
        zs_type: str,
        lines: List[LINE],
        max_line_num: int = 999,
        zs_include_last_line=True,
        ) -> List[ZS]:
        """一次性计算中枢，bi / xd 都用临时 ZsCalculator 实例保证口径一致。

        max_line_num / zs_include_last_line 在新实现里无对应语义，保留仅为
        兼容老调用方。本方法是无状态纯函数式调用，每次新建计算器即可。
        """
        if not lines:
            return []
        tmp_calc = ZsCalculator()
        return tmp_calc.calculate(lines)

    # --- 兼容属性与方法 ---
    @property
    def idx(self) -> dict:
        return self.macd_calculator.get_results()

    @property
    def src_klines(self) -> List[Kline]:
        return self.kline_processor.klines

    @property
    def cl_klines(self) -> List[CLKline]:
        return self.cl_kline_processor.cl_klines

    @property
    def fxs(self) -> List[FX]:
        return self.bi_calculator.fxs

    @property
    def bis(self) -> List[BI]:
        return self.bi_calculator.bis

    @property
    def xds(self) -> List[XD]:
        return self.xd_calculator.xds

    @property
    def last_bi_zs(self) -> Union[ZS, None]:
        return self.get_last_bi_zs()

    @property
    def last_xd_zs(self) -> Union[ZS, None]:
        return self.get_last_xd_zs()

    @property
    def type_bi_zss(self) -> dict:
        return {Config.ZS_TYPE_BZ.value: self.get_bi_zss(Config.ZS_TYPE_BZ.value)}

    @property
    def type_xd_zss(self) -> dict:
        return {Config.ZS_TYPE_BZ.value: self.get_xd_zss(Config.ZS_TYPE_BZ.value)}

    def default_bi_zs_type(self) -> str:
        return self.config.get('zs_type_bi', Config.ZS_TYPE_BZ.value)

    def default_xd_zs_type(self) -> str:
        return self.config.get('zs_type_xd', Config.ZS_TYPE_BZ.value)

    def write_debug_log(self, msg: str):
        if self.debug:
            LogUtil.debug(msg)

    def _add_time(self, key: str, value: float):
        self.use_time[key] = value

    # --- process_xxx 系列：手工分步触发的兼容入口 ---
    # 保留给历史调用方（notebook、单测、外部脚本）。子计算器内部已做脏检查，
    # 多次调用幂等。这里只做 thin wrapper，不再额外缓存判断，避免双重判定。

    def process_idx(self):
        self.macd_calculator.process_macd(self.kline_processor.klines)
        self._compute_htf_macd()
        return self

    def process_fx(self):
        # fxs 现在由 BiCalculator 一并产出，没有独立的 fx 阶段。
        self.bi_calculator.calculate(self.cl_kline_processor.cl_klines)
        return self

    def process_bi(self):
        self.bi_calculator.calculate(self.cl_kline_processor.cl_klines)
        return self

    def process_up_line(self):
        self.xd_calculator.calculate(self.bi_calculator.bis)
        return self

    def process_zs(self):
        self.zss_calculator.calculate(self.xd_calculator.xds)
        # 中枢有变化时，最后中枢缓存必须失效，
        # 否则下次 last_xd_zs 仍是旧值。
        self._last_xd_zs = None
        self._last_bi_zs = None
        return self

    @staticmethod
    def _calc_layer_sig(lines) -> tuple:
        """构造单一 line 层（xds 或 bis）的尾部签名，供 xd / bi 两层共用。

        签名任一项变化都意味着 BsPointCalculator 需要重新扫描：
        长度变化=新增段；末段端点 K 或价格变化=pending 笔/线段实时漂移；
        done 翻转=pending 段与 confirmed 互转。
        """
        if not lines:
            return (0, -1, -1, None, None, None, None, False)
        last_line = lines[-1]
        start_k_idx = -1
        end_k_idx = -1
        start_val = None
        end_val = None
        try:
            if last_line.start is not None:
                start_val = last_line.start.val
                if last_line.start.k is not None:
                    start_k_idx = last_line.start.k.k_index
            if last_line.end is not None and last_line.end.k is not None:
                end_k_idx = last_line.end.k.k_index
            if last_line.end is not None:
                end_val = last_line.end.val
        except AttributeError:
            start_k_idx = -1
            end_k_idx = -1
            start_val = None
            end_val = None
        return (
            len(lines),
            start_k_idx,
            end_k_idx,
            start_val,
            end_val,
            getattr(last_line, 'high', None),
            getattr(last_line, 'low', None),
            bool(getattr(last_line, 'done', False)),
        )

    def process_mmd(self):
        """
        计算三类买卖点（1buy/1sell, 2buy/2sell, 3buy/3sell）。

        笔层（zs_type='bi'）与线段层（zs_type='xd'）对称接入 BsPointCalculator，
        共用同一识别引擎，仅输入不同（xd 层用 xds + xd 中枢，bi 层用 bis +
        bi 中枢）。get_bi_zss() / get_xd_zss() 都含 pending_zs，不丢末段买卖点。

        **执行顺序**: bi 层先于 xd 层——原文定律一(kobo.54.1)「任何级别的第二
        类买卖点都由次级别相应走势的第一类买点构成」要求 xd 层 2 类时能读到
        bi 层 1 类(BsPointCalculator 据此走定律一路径)。bi 层无更细次级别,
        2 类走经验法兜底。任一层异常由 process_klines 外层 except 统一清理。
        """
        from chanlun.core.bs_point_calculator import BsPointCalculator

        # --- 笔层(必须先跑,供 xd 层 2 类的定律一路径读取) ---
        bis = self.bi_calculator.bis
        bi_zss = self.get_bi_zss()
        if bis and bi_zss:
            BsPointCalculator(self, zs_type='bi').calculate(bis, bi_zss)

        # --- 线段层 ---
        xds = self.xd_calculator.xds
        # 必须用 get_xd_zss()（含 pending_zs），zss_calculator.zss 只含已完成
        # 中枢，但趋势背驰常发生在最后一个中枢未完成时，否则末段买卖点会丢。
        xd_zss = self.get_xd_zss()
        if xds and xd_zss:
            BsPointCalculator(self, zs_type='xd').calculate(xds, xd_zss)

        return self
