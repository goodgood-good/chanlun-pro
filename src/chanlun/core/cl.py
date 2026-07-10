# -*- coding: utf-8 -*-
import datetime
from typing import Union, List, Tuple, Any
import pandas as pd

from chanlun.core.bi_calculator import BiCalculator
# 笔/段两层中枢统一走 ZsCalculator，create_dn_zs 也用临时 ZsCalculator 实例。
from chanlun.core.types import ICL, Kline, CLKline, FX, BI, XD, ZS, ZSLX, Config, LINE, query_macd_ld
from chanlun.core.zslx_calculator import ZslxCalculator
from chanlun.core import beichi_calculator as bc
from chanlun.core.cl_kline_process import CL_Kline_Process
from chanlun.core.kline_data_processor import KlineDataProcessor
from chanlun.core.macd import MACD
from chanlun.core.macd_htf import HigherMACDCalculator
from chanlun.core.xd_calculator import XdCalculator
from chanlun.core.zs_calculator import ZsCalculator
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
                偏移；1m→5m、5m→30m 纯按时间戳重采样，不依赖此值。未显式传时
                按代码后缀推断（.US→us，其余→a），使非 web 路径也得正确时区。
        """
        self.code = code
        self.frequency = frequency
        self.config = config if config else {}
        self.start_datetime = start_datetime
        # market 未显式传时按代码后缀推断(仅高周期 MACD 日级分桶时区用):美股 .US→us
        # (offset -5),其余→a(+8,与历史 market=None 默认一致零回归);web 显式传不受影响。
        self.market = market if market is not None else (
            "us" if str(code or "").upper().endswith(".US") else "a"
        )

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
        self._htf_macd_calculator: Union[HigherMACDCalculator, None] = None
        # 递归装配结果缓存:get_recursive_branch_levels / get_branch_bspoints /
        # get_kuozhan_levels / get_kuozhan_candidates 是无状态全量重算且互相调用,
        # 一次回放信号采集里会被多个采集器重复触发。同一 CL 状态下结果恒等 → 按状态
        # 缓存,新 K 线进来(_process_src_klines)即清空。消除单次采集内的冗余重算,
        # 结果不变、纯加速。
        self._recursive_memo: dict = {}
        # 实例化笔计算器
        self.bi_calculator = BiCalculator(bi_mode = 'strict')
        # 实例化线段计算器
        self.xd_calculator = XdCalculator(self.config)

        # 成枢门统一(P1a, 2026-07-06):legacy 两层与递归链同用单点解析(现=5)——落实并
        # 扩展 7c231295 拍板意图(曾同步笔层=5、被 3737497a 重构无声回退;本次线段层一并
        # 统一,消除同一 lines 两套中枢)。封顶 max=8 维持 legacy 显示口径不变(递归链不封顶
        # 的理由见 zs_branch;此处 8 段封顶只影响观察层/legacy mmd,不入递归)。
        _l0_min = self._recursive_l0_min_zs_lines()
        self.zss_calculator = ZsCalculator(allow_tail_noop=True, min_zs_lines=_l0_min)
        # 笔层中枢计算器，与 zss_calculator（线段层）独立维护。
        self.bi_zss_calculator = ZsCalculator(allow_tail_noop=True, min_zs_lines=_l0_min)

        # 走势类型计算器：线段中枢 → 走势类型(ZSLX)，无状态全量重算。
        self.zslx_calculator = ZslxCalculator()
        self.xd_zslx: List[ZSLX] = []  # 线段级走势类型列表

        # 最后中枢缓存
        self._last_bi_zs: Union[ZS, None] = None
        self._last_xd_zs: Union[ZS, None] = None

        # process_mmd 触发签名缓存：仅当 xds + bis 尾部签名（长度/末段 K 索引/末段
        # done 状态）变化才重跑，避免每根 K 线全量扫描 1B/2B/3B（O(N²)）。
        # 签名同时覆盖 xd / bi 尾部，任一层变化都需重跑。
        self._last_mmd_sig: Union[tuple, None] = None

        # bs_point 买卖点引擎持久实例(bi/xd 两层),跨 process_mmd 复用以承载增量
        # 状态。惰性创建(避开 cl<->bs_point 循环 import)。calculate 现为无状态全量
        # 重算,复用实例与每次 new 行为完全一致(纯分配优化)。
        self._bi_bsp = None
        self._xd_bsp = None
        # 递归层持久 RecursiveBranchCalculator,按「塔」(use_xd:False=笔/True=段)分实例
        # (各塔输入不同 → 各自 identity 稳定、不互相抖动),跨 K 复用承载递归层增量状态。
        self._rbc: dict = {}

        # 兼容运行时期望字段
        self.debug: bool = False

    def __setstate__(self, state: dict):
        """unpickle/deepcopy 还原钩子(R1-F1-2)。

        修复(.US 后缀推断)只在 __init__,而 cl_object_cache 的 pkl 续算谱系绕过
        __init__:修复前落盘的美股 CL(market=None)将无限期用 +8 日级分桶(应 -5),
        且 get_web_cl_data 每请求重写 pkl 刷新 mtime,15 天清理永不触发。此处按同
        规则补推断治愈遗留对象;A股 None→"a" 与历史默认等价零回归;显式 market 原样。
        治愈后首个 process_klines 因 calc.market != self.market 自动重建 HTF 计算器。"""
        self.__dict__.update(state)
        if self.__dict__.get("market") is None:
            code = self.__dict__.get("code")
            self.market = "us" if str(code or "").upper().endswith(".US") else "a"

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
            # 中枢位置关系 / 趋势判定档:
            #   - GD (默认): 较严格档——要求前后中枢连同其波动包络(GG/DD)完全
            #     无交集才算趋势;核心区间分离但 GG/DD 重叠视为中枢扩展、非趋势。
            #   - ZGD: 仅比中枢核心区间 zd/zg(后 zd>前 zg 即趋势),口径较宽——
            #     会把判为「中枢扩展」的情形也当趋势,实务信号更多。
            #   - ZGGDD: 兼容老用法,zg 与 dd、zd 与 gg 比较,介于两者之间。
            # 注:本配置同时驱动 buy/sell 点链路 (cl.beichi_qs / zss_is_qs)
            # 与递归链路 (走势类型划分 / recursive / interval_nest),
            # 见 process_klines / get_recursive_branch_levels / get_branch_interval_nest。
            'zs_wzgx': Config.ZS_WZGX_GD.value,
            'cal_last_zs': True,
            'use_macd_ld': True,
            # 背驰力度判断用高一周期 MACD（在高一级别上度量力度：
            # 1m→5m、5m→30m…）。开关关 / 无高周期 / 高周期桶不足时
            # query_macd_ld 自动回退原生 MACD。
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
        return self._process_src_klines(src_klines)

    def process_kline_values(self, date, open_, high, low, close, volume=0.0):
        """Process one normalized bar without constructing a DataFrame."""
        src_klines: List[Kline] = self.kline_processor.process_kline_values(
            date, open_, high, low, close, volume
        )
        return self._process_src_klines(src_klines)

    def _process_src_klines(self, src_klines: List[Kline]):
        if not src_klines:
            return self
        self._recursive_memo.clear()   # 新 K 线 → 状态变,递归装配 memo 失效
        # 线段确认级联重构期间:线段(xds)改为全量重建(对象每轮全新、终点可回溯合并),
        # 持久 RecursiveBranchCalculator 的 identity 增量前提失效 → 每轮重置为全量重算,
        # 保递归层 inc==batch。待级联稳定后与线段增量一并重做。
        self._rbc.clear()

        try:
            # 直接引用内部数据，避免 deepcopy
            # 使用MACD计算器更新指标
            self.macd_calculator.process_macd(self.kline_processor.klines)
            # 高周期 MACD：背驰力度在高一级别上度量。每轮全量重算。
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

            skip_legacy_zslx = self.config.get('skip_legacy_zslx', False)
            # 笔层中枢与线段层对称接入，主流程自动算。升级链 walk-forward
            # 回放不消费 legacy bi_zss/xd_zslx，可显式跳过以保留无未来语义并降低成本。
            if not skip_legacy_zslx:
                self.bi_zss_calculator.calculate(self.bi_calculator.bis)

            # 走势类型划分：用线段中枢 + 背驰划出走势类型，并回填
            # 线段中枢的 zs.type（上涨/下跌中枢 up/down、盘整中枢 zd）。
            xd_zss = list(self.zss_calculator.zss)
            if self.zss_calculator.pending_zs is not None:
                xd_zss.append(self.zss_calculator.pending_zs)
            ld_provider = lambda s, e: query_macd_ld(self, s, e)
            # 递归链路（走势类型 / 递归 / 区间套）默认走较严格档 GD：
            # 趋势判定按 GG/DD 包络口径（后GG<前DD → 下跌、后DD>前GG → 上涨）。
            # 递归链路、买卖点链路统一读 config,保证用户显式 opt-out 到 ZGGDD
            # 等较宽档时所有链路口径一致,避免「买卖点说趋势 / ZSLX 说盘整」
            # 的双口径冲突。
            recursive_wzgx = self._recursive_wzgx()
            if not skip_legacy_zslx:
                self.xd_zslx = self.zslx_calculator.calculate(
                    xd_zss, self.xd_calculator.xds, ld_provider, recursive_wzgx,
                    self.frequency,
                )
            else:
                self.xd_zslx = []

            # 笔中枢方向回填：用同一套走势类型划分给笔层中枢回填方向(上涨/下跌
            # 中枢 up/down、盘整中枢 zd)。下游 cl_analyse 等用 zs.type 做 up/down
            # 二分逻辑时,笔中枢拿到正确方向(此前是 ZsCalculator 内部置的 seg_b.type
            # 占位)。仅用副作用(回填),不存储 bi_zslx——bi 层走势类型尚无消费方。
            bi_zss = []
            if not skip_legacy_zslx:
                bi_zss = list(self.bi_zss_calculator.zss)
                if self.bi_zss_calculator.pending_zs is not None:
                    bi_zss.append(self.bi_zss_calculator.pending_zs)
            if bi_zss:
                # bi 流仅需「中枢方向回填」副作用(走势类型返回值不消费,见上注释),直调
                # 纯回填、不跑状态机/签名/缓存——回填是 per-zs(核心首段反向)、与完整
                # zslx 等价(见 backfill_zs_directions),省去 bi 流全部 zslx 开销。
                self.zslx_calculator.backfill_zs_directions(bi_zss)

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
            if self.config.get('skip_legacy_mmd', False):
                self._last_mmd_sig = new_sig
            elif new_sig != self._last_mmd_sig:
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

        背驰力度在「高一级别」上度量。开关 ``macd_ld_use_htf`` 关、无高周期
        对照、或高周期 K 线桶不足时置 None —— query_macd_ld 据此回退原生 MACD。
        """
        flag = self.config.get('macd_ld_use_htf', True)
        # 兼容 bool / 字符串("0"/"1") 两种配置写法
        if flag in (False, 0, '0', 'false', 'False', ''):
            self._htf_macd = None
            return
        fast = int(self.config.get('idx_macd_fast', 12))
        slow = int(self.config.get('idx_macd_slow', 26))
        signal = int(self.config.get('idx_macd_signal', 9))
        calc = self._htf_macd_calculator
        if (
            calc is None
            or calc.frequency != self.frequency
            or calc.market != self.market
            or calc.fast != fast
            or calc.slow != slow
            or calc.signal != signal
        ):
            calc = HigherMACDCalculator(
                self.frequency,
                self.market,
                fast=fast,
                slow=slow,
                signal=signal,
            )
            self._htf_macd_calculator = calc
        self._htf_macd = calc.update(self.kline_processor.klines)

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
        """返回线段级走势类型列表。"""
        return self.xd_zslx

    # ===== 新核心模块 lazy 接入(并存,不动旧版/process_klines/golden) =====
    def _recursive_wzgx(self) -> str:
        """统一解析中枢位置关系口径(zs_wzgx):所有链路必须经此单点读取。

        fallback=较严格档 GD(趋势延续按 GG/DD 包络比较:后DD>前GG=上涨延续、
        后GG<前DD=下跌延续)。任何方法不得自带 fallback——保证同一 CL 对象内
        所有链路口径一致,避免 config 缺键时图表与回测对趋势/盘整结论矛盾。
        """
        return self.config.get('zs_wzgx', Config.ZS_WZGX_GD.value)

    def _recursive_l0_min_zs_lines(self) -> int:
        """统一解析递归 L0 中枢成枢线数:所有链路必须经此单点读取。

        fallback=5(用户口径 2026-06-26 拍板 4→5, commit 7c231295)。精确语义:ZsCalculator 的
        min_zs_lines = center.lines 长度下限,而 lines **不含进入段**(进入段在 zs.start)——
        故 5 = 与核心区重叠的段数(含离开段)≥5,比 7c231295 注释宣称的「进入+核心+离开=5」
        实际严一段;golden/回测基线均已固化在该行为上,若要按字面意图放宽须另行拍板并联动重跑。
        递归链(recursive_branch/recursive_calculator)**全级别统一**用本值——L≥1 走势类型
        中枢同为 5(历史曾用 3,旧注释勿信)。
        成枢门已统一(P1a):legacy 线段/笔中枢(zss_calculator/bi_zss_calculator, 见 __init__:80,82)
        与递归链同用本单点值 min=5+封顶 8——落实 7c231295 拍板意图(曾同步笔层=5、被 3737497a
        重构无声回退,现已一并统一;audit/zhihu_lizhengli_audit.md F1 的口径分裂已收敛)。
        ⚠ 改成枢门 → 信号/买卖点变 → golden 须重生成 + 信号缓存版本 +1 + 回测基线须重跑。
        """
        return int(self.config.get('recursive_l0_min_zs_lines', 5) or 5)

    def _recursive_branch_calc(self, use_xd: bool):
        """按塔(use_xd:False=笔/True=段)取 RecursiveBranchCalculator。设计上支持递归层增量(各级持久子
        calculator 跨 K 复用增量状态),但当前 _process_src_klines 每 K 调 self._rbc.clear()(见 :192,
        线段级联重构期临时措施、保 inc==batch)→ 实例与增量状态实际每 K 重置为全量重算、不跨 K
        保留,待线段增量稳定后恢复。各塔输入不同 → 各自 identity 稳定、不互相抖动。"""
        rbc = self._rbc.get(use_xd)
        if rbc is None:
            from chanlun.core.recursive_branch import RecursiveBranchCalculator
            rbc = RecursiveBranchCalculator(
                l0_min_zs_lines=self._recursive_l0_min_zs_lines(),
                pending_min_lines=self.config.get("recursive_pending_min_lines"),  # O2:None=零行为,3=原文披露口径(仅影响 live/pending/nest 介入,done 链不变)
            )
            self._rbc[use_xd] = rbc
        return rbc

    def get_recursive_branch_levels(self):
        """新核心:recursive_branch 多级递归(中枢+内联背驰+走势类型)。

        旧链(get_recursive_levels/RecursiveCalculator)已下线(审计 P1b);本方法走重做的
        zs_branch/zslx_branch/recursive_branch 链路,wzgx 统一读 config(fallback=
        较严格档 GD,见 _recursive_wzgx)。
        lazy 现算、不接 process_klines。返回 List[recursive_branch.LevelResult]。
        """
        cached = self._recursive_memo.get("rbl")
        if cached is not None:
            return cached
        ld = lambda s, e: query_macd_ld(self, s, e)
        wzgx = self._recursive_wzgx()
        # L0 输入用线段(xds):线段中枢是最低正式级别中枢,升级链由此起。
        # 笔中枢是更小的观察级别、不参与升级,单独走 get_bi_zhongshu。
        # (1m 标的线段稀疏→线段中枢少;丰富的笔中枢另在观察层显示。)
        result = self._recursive_branch_calc(True).calculate(
            list(self.get_xds()), ld, wzgx, self.frequency,
            zs_diversity=bool(self.config.get("recursive_zs_diversity", False)),
        )
        # 升级由自然递归忠实产出:每级都过 zs_diversity.refine,L1 中枢 = 次级别走势类型重叠
        # (核心=走势类型重叠、锚定子中枢),对齐原文「新中枢从最后一个次级别中枢起算」。
        # 旧 emit_l1_upgrades 旁路(把单个 L0 延伸巨枢同核心 relabel 成 L1)违此口径——单个盘整
        # 巨枢不能自成高级别中枢,须 3 个走势类型重叠才升级——已移除(实证:它在自然 L1 之上
        # 多塞 6 个同核心单枢,均为伪升级)。
        self._recursive_memo["rbl"] = result
        return result

    def get_bi_zhongshu(self):
        """笔中枢:新核心 zs_branch 直接在笔(bis)上找的中枢。

        笔中枢是比线段中枢更小的「观察级别」,不参与升级链(升级从线段中枢起,
        见 get_recursive_branch_levels L0=线段)。返回 List[ZS]。
        """
        from chanlun.core.zs_branch import ZsBranchCalculator
        ld = lambda s, e: query_macd_ld(self, s, e)
        wzgx = self._recursive_wzgx()
        res = ZsBranchCalculator(
            ld_provider=ld, frequency=self.frequency, wzgx=wzgx, min_zs_lines=5,  # 用户口径: 进入+核心+离开≥5
        ).calculate(list(self.get_bis()))
        return res.done_zss

    def get_branch_bspoints(self, use_xd: bool = False):
        """新核心:一/二/三类买卖点(全多级)。lazy 并存。返回 List[BuySellPoint]。

        L0 一三类(bs_branch 单级)+ 二类(bs2 跨级)+ L1+ 扩张三买(bs3,过滤 L0 去重)。
        一类多级(L1+ 背驰一类)留后。

        ``use_xd``:构成段选择——False=**笔**(细粒度,图表「笔买卖点」bi_mmds);
        True=**线段**(图表「段买卖点」xd_mmds,与线段中枢同级)。两级各算、各归其位。
        """
        memo_key = ("bsp", bool(use_xd))
        cached = self._recursive_memo.get(memo_key)
        if cached is not None:
            return cached
        from chanlun.core.zs_branch import ZsBranchResult
        from chanlun.core.bs_branch import BsBranchCalculator
        from chanlun.core.bs2_branch import Bs2BranchCalculator
        from chanlun.core.bs3_branch import Bs3BranchCalculator
        ld = lambda s, e: query_macd_ld(self, s, e)
        wzgx = self._recursive_wzgx()
        units = list(self.get_xds()) if use_xd else list(self.get_bis())
        levels = self._recursive_branch_calc(use_xd).calculate(
            units, ld, wzgx, self.frequency,
            zs_diversity=bool(self.config.get("recursive_zs_diversity", False)),
        )
        if not levels:
            self._recursive_memo[memo_key] = []
            return []
        l0 = levels[0]
        zr0 = ZsBranchResult(done_zss=l0.zss, live=[], freeze_idx=0,
                             done_divergence=l0.done_divergence)
        bs = BsBranchCalculator()
        pts = list(bs.calculate(zr0, l0.units))                            # L0 一三类
        pts += bs.second_class(zr0, l0.units, ld, self.frequency)          # L0 二类(强/一般档 + 最弱档破前低+盘整背驰)
        pts += Bs2BranchCalculator().calculate(levels, ld, self.frequency)  # L1+ 二类(跨级 + 最弱档)
        pts += [p for p in Bs3BranchCalculator().calculate(levels)
                if p.level is not None and p.level >= 1]                   # L1+ 扩张三买(不重 L0)
        self._recursive_memo[memo_key] = pts
        return pts

    def get_branch_dingli2_upgrades(self, use_xd: bool = False):
        """定理二升级中枢·独立 done 通道(升级实体化立项第一期,方案 b)。lazy 并存。

        返回 List[(level, ZS)]:各级已确认(离开确认)的升级中枢,zs._dingli2_upgrade=True。
        不入 zss/递归上行/默认信号白名单——图表独立显示与二期回测对照用(设计文档
        docs/superpowers/specs/2026-07-06-upgrade-materialization-design.md)。
        """
        memo_key = ("dingli2_up", bool(use_xd))
        cached = self._recursive_memo.get(memo_key)
        if cached is not None:
            return cached
        ld = lambda s, e: query_macd_ld(self, s, e)      # noqa: E731
        wzgx = self._recursive_wzgx()
        units = list(self.get_xds()) if use_xd else list(self.get_bis())
        levels = self._recursive_branch_calc(use_xd).calculate(units, ld, wzgx, self.frequency)
        out = [(lv.level, z) for lv in levels for z in getattr(lv, "upgrade_zss", []) or []]
        self._recursive_memo[memo_key] = out
        return out

    def get_branch_dingli2_bspoints(self, use_xd: bool = False):
        """升级中枢三类买卖点(升级实体化二期第一步,独立通道)。lazy 并存。

        返回 List[BuySellPoint](p.level=升级中枢所在级,p.zs._dingli2_upgrade=True)。
        不入默认信号白名单;绩效接入待 engine 采集层对照。units 用升级中枢构成段
        所在层(L_{k-1})的段序列。
        """
        memo_key = ("dingli2_bsp", bool(use_xd))
        cached = self._recursive_memo.get(memo_key)
        if cached is not None:
            return cached
        from chanlun.core.recursive_branch import dingli2_third_class
        ld = lambda s, e: query_macd_ld(self, s, e)      # noqa: E731
        wzgx = self._recursive_wzgx()
        units0 = list(self.get_xds()) if use_xd else list(self.get_bis())
        levels = self._recursive_branch_calc(use_xd).calculate(units0, ld, wzgx, self.frequency)
        by_level = {lv.level: lv for lv in levels}
        out = []
        for lv in levels:
            ups = getattr(lv, "upgrade_zss", None) or []
            if not ups:
                continue
            base = by_level.get(lv.level - 1)
            lines = list(base.units) if base is not None else units0
            for pt in dingli2_third_class(ups, lines):
                pt.level = lv.level
                out.append(pt)
        self._recursive_memo[memo_key] = out
        return out

    def get_branch_quasi_first(self, use_xd: bool = False):
        """新核心:类一买/类一卖(大级别盘整背驰=历史性底部=类买点)。lazy 并存。
        返回 List[BuySellPoint](bs_type='类1buy'/'类1sell')。

        ★类=quasi(操作意义弱、需次级别确认):刻意**不入** get_branch_bspoints/回测
        BUYS 白名单(engine.buy_class 取首字符 int,'类'前缀会崩),作单独 marker 暴露,供图表/
        分析/可选用,不改默认信号/golden/回测。``use_xd`` 同 get_branch_bspoints。
        """
        memo_key = ("quasi_first", bool(use_xd))
        cached = self._recursive_memo.get(memo_key)
        if cached is not None:
            return cached
        from chanlun.core.bs1_branch import QuasiFirstClassCalculator
        ld = lambda s, e: query_macd_ld(self, s, e)      # noqa: E731
        wzgx = self._recursive_wzgx()
        units = list(self.get_xds()) if use_xd else list(self.get_bis())
        levels = self._recursive_branch_calc(use_xd).calculate(units, ld, wzgx, self.frequency)
        out = QuasiFirstClassCalculator().calculate(levels)
        self._recursive_memo[memo_key] = out
        return out

    def get_branch_bcs(self, use_xd: bool = False):
        """新核心背驰段(各级 done_divergence 里 is_beichi 的离开段),供图表「背驰信号」显示。
        返回 [(date, price, kind)],kind='qs'(趋势背驰)/'pz'(盘整背驰)。``use_xd`` 同
        get_branch_bspoints:False=笔级→笔背驰(bi_bcs)、True=线段级→段背驰(xd_bcs)。
        原图表背驰信号走 legacy line_bcs(极稀疏)、与新核心买卖点不一致;接此后两者同源(2026-06)。
        """
        from chanlun.core.recursive_branch import RecursiveBranchCalculator
        ld = lambda s, e: query_macd_ld(self, s, e)
        wzgx = self._recursive_wzgx()
        l0_min = self._recursive_l0_min_zs_lines()
        units = list(self.get_xds()) if use_xd else list(self.get_bis())
        levels = RecursiveBranchCalculator(l0_min_zs_lines=l0_min).calculate(
            units, ld, wzgx, self.frequency,
        )
        out = []
        for lv in levels:
            for dv in lv.done_divergence:
                if dv is None or not dv.is_beichi or dv.leave_seg is None:
                    continue
                seg = dv.leave_seg
                if seg.end is None:
                    continue
                out.append((seg.end.k.date, seg.end.val, dv.kind))
        return out

    # 升级链(封顶 30m 操作级):各 base 频率 → [(目标频率, 方法)]。
    # <30m=kuozhan(延伸/扩展);30m=tongjibie(同级别分解,恰好3段走势类型重合不延伸)。
    _UPGRADE_CHAIN = {
        "1m": [("5m", "kuozhan"), ("30m", "tongjibie")],
        "5m": [("30m", "tongjibie")],
    }

    def get_kuozhan_levels(self):
        """递归升级各级中枢 + 背驰 + 买卖点(带 level)。封顶 30m(操作级)。

        升级层走 `get_recursive_branch_levels` 的走势类型递归。**保留本接口签名**
        (tv_chart 调用),内部走走势类型递归。
        - **<30m kuozhan 级 = 走势类型递归 L1/L2 中枢** + 买卖点 bs1(L1+一类)/bs2(二类)/
          bs3(扩张三买);背驰段 = 该级 done_divergence。
        - **30m tongjibie 级 = 同级别分解**(上级走势类型 zslxs,去摆动腿直用走势类型,
          恰好 3 段重合、不延伸、允许盘整+盘整);买卖点 tongjibie_level_signals。
        - 30m 以上不考虑。30m/日线图无升级链 → 返回 []。
        频率标签(5m/30m)= 递归 depth 的显示名(近似映射)。返回
        List[dict]:[{level, zss, bsp(List[BuySellPoint]), bcs(List[(date,val,kind)]), lower}]。
        """
        from collections import defaultdict

        chain = self._UPGRADE_CHAIN.get(self.frequency, [])
        if not chain:
            return []
        cached = self._recursive_memo.get("kuozhan_levels")
        if cached is not None:
            return cached
        from chanlun.core.bs1_branch import Bs1BranchCalculator
        from chanlun.core.bs2_branch import Bs2BranchCalculator
        from chanlun.core.bs3_branch import Bs3BranchCalculator
        from chanlun.core.zs_upgrade import tongjibie_zhongshu_ex, tongjibie_level_signals
        levels = self.get_recursive_branch_levels()
        if not levels:
            return []
        by_blevel = {lv.level: lv for lv in levels}
        if 0 not in by_blevel:
            return []
        xds = list(self.get_xds())
        ld = lambda s, e: query_macd_ld(self, s, e)      # noqa: E731
        wzgx = self._recursive_wzgx()
        # 多级升级买卖点(bs1 L1+一类 + bs2 二类 + bs3 扩张三买),按递归 level 分组
        # C13: bs2 传 ld/frequency 才产最弱档二类(破前低+盘整背驰), 与信号链
        # get_branch_bspoints 口径一致——否则升级级图层缺最弱档, 与实际成交信号点分裂。
        branch_bsp = (Bs1BranchCalculator().calculate(levels)
                      + Bs2BranchCalculator().calculate(levels, ld, self.frequency)
                      + Bs3BranchCalculator().calculate(levels))
        bsp_by_blevel = defaultdict(list)
        for p in branch_bsp:
            if p.level is not None:
                bsp_by_blevel[p.level].append(p)
        import logging
        _log = logging.getLogger(__name__)
        out = []
        blockr_level = 0                                  # 当前图频率线段中枢所在递归级别
        cur_zslxs = list(by_blevel[0].zslxs or [])        # 当前级别走势类型(tongjibie 的次级别单位)
        for lvl, (_target, method) in enumerate(chain, start=1):
            nz, bsp, bcs = [], [], []
            lower = cur_zslxs
            try:
                if method == "kuozhan":                   # 走势类型递归升一级
                    blockr_level += 1
                    lv = by_blevel.get(blockr_level)
                    if lv is not None:
                        nz = list(lv.zss)
                        # dingli2 升级中枢·语境标注(audit §十三定位:图表语境、非交易
                        # 触发):done 升级带并入本级中枢显示(生成时已与自然递归中枢时间
                        # 去重 D3);买卖点刻意**不并入** bsp——画交易标记违背该裁决。
                        nz += list(getattr(lv, "upgrade_zss", None) or [])
                        cur_zslxs = list(lv.zslxs or [])
                        bsp = list(bsp_by_blevel.get(blockr_level, []))
                        bcs = [
                            (dv.leave_seg.end.k.date, dv.leave_seg.end.val, dv.kind)
                            for dv in (lv.done_divergence or [])
                            if dv is not None and dv.is_beichi
                            and dv.leave_seg is not None and dv.leave_seg.end is not None
                        ]
                    else:
                        cur_zslxs = []
                elif method == "tongjibie":               # 同级别分解(上级走势类型)
                    nz, meta = tongjibie_zhongshu_ex(cur_zslxs, xds)
                    bsp, bcs = tongjibie_level_signals(nz, meta, ld, wzgx, self.frequency)
                for p in bsp:
                    p.level = lvl
            except Exception:
                _log.warning("get_kuozhan_levels L%d(%s) 失败", lvl, method, exc_info=True)
                nz, bsp, bcs = [], [], []
            # **即使本级空也出层** → 前端菜单反映升级链(该周期可用级别)。
            out.append({"level": lvl, "zss": nz, "bsp": bsp, "bcs": bcs, "lower": lower})
        self._recursive_memo["kuozhan_levels"] = out
        return out

    def get_kuozhan_candidates(self):
        """区间套介入候选。已弃用:区间套介入统一走走势类型递归——
        `engine.collect_qs_beichi_candidates` 用 `LevelResult.live_qs_divergence`
        (右边缘进行中趋势背驰段)产 1buy_nest 介入。
        保留接口、恒返回 [](nest_cascade 模式现走 collect_qs_beichi)。"""
        return []

    def get_xiaozhuanda_candidates(self):
        """新核心:小转大(小背驰-大转折)候选/预警(只读结构,不接交易信号)。lazy。
        返回 List[XiaoZhuanDaCandidate]。

        判定:大级别走势最后一个次级别中枢出现三类点为必要(非充分)条件
        → 输出候选/预警、非定性买卖点。基于多级 LevelResult。
        当下:只用 done 中枢 + 当下三类点。
        """
        cached = self._recursive_memo.get("xzd_candidates")
        if cached is not None:
            return cached
        from chanlun.core.xiaozhuanda_branch import XiaoZhuanDaCalculator
        out = XiaoZhuanDaCalculator().calculate(self.get_recursive_branch_levels())
        self._recursive_memo["xzd_candidates"] = out
        return out

    def get_branch_interval_nest(self):
        """新核心:区间套可操作性(自顶向下 READ)。lazy 并存。返回 List[NestRead]。"""
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
        """判断中枢与指定线是否构成盘整背驰(进入段 vs 离开段;薄壳,见 beichi_calculator)。"""
        ld_provider = lambda s, e: query_macd_ld(self, s, e)
        return bc.beichi_pz(zs, now_line, ld_provider, self.frequency)

    def beichi_zs_oscillation(self, zs: ZS, now_line: LINE) -> Tuple[bool, Union[LINE, None]]:
        """中枢震荡力度比较(当下段 vs 中枢内最近同向段;原 beichi_pz 旧语义薄壳)。"""
        ld_provider = lambda s, e: query_macd_ld(self, s, e)
        return bc.beichi_zs_oscillation(zs, now_line, ld_provider, self.frequency)

    def beichi_qs(
            self, lines: List[LINE], zss: List[ZS], now_line: LINE
    ) -> Tuple[bool, List[LINE]]:
        """判断指定线与之前的中枢是否形成趋势背驰（薄壳，逻辑见 beichi_calculator）。"""
        ld_provider = lambda s, e: query_macd_ld(self, s, e)
        wzgx_config = self._recursive_wzgx()
        # 买卖点链路用「中枢本体包络」判趋势(剔除波动/延伸/离开段)——否则远摆撑爆
        # 包络、趋势恒判不出、1类不触发(见 combination_calculator.core_envelope)。
        return bc.beichi_qs(
            lines, zss, now_line, ld_provider, wzgx_config, self.frequency,
            use_core_envelope=True,
        )

    def zss_is_qs(self, one_zs: ZS, two_zs: ZS) -> Union[str, None]:
        """判断两个中枢是否形成趋势（薄壳，逻辑见 beichi_calculator）。"""
        wzgx_config = self._recursive_wzgx()
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
        tmp_calc = ZsCalculator(min_zs_lines=self._recursive_l0_min_zs_lines())  # 成枢门单点统一(P1a)
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

        **执行顺序**: bi 层先于 xd 层——任何级别的第二类买卖点由次级别相应走势
        的第一类买点构成,要求 xd 层 2 类时能读到 bi 层 1 类(BsPointCalculator
        据此走对应路径)。bi 层无更细次级别,2 类走经验法兜底。任一层异常由
        process_klines 外层 except 统一清理。
        """
        # bs_point 引擎用持久实例(承载增量状态);惰性创建避开 cl<->bs_point 循环 import。
        if self._bi_bsp is None:
            from chanlun.core.bs_point_calculator import BsPointCalculator
            self._bi_bsp = BsPointCalculator(self, zs_type='bi')
            self._xd_bsp = BsPointCalculator(self, zs_type='xd')

        # --- 笔层(必须先跑,供 xd 层 2 类读取笔层 1 类) ---
        bis = self.bi_calculator.bis
        bi_zss = self.get_bi_zss()
        if bis and bi_zss:
            self._bi_bsp.calculate(bis, bi_zss)

        # --- 线段层 ---
        xds = self.xd_calculator.xds
        # 必须用 get_xd_zss()（含 pending_zs），zss_calculator.zss 只含已完成
        # 中枢，但趋势背驰常发生在最后一个中枢未完成时，否则末段买卖点会丢。
        xd_zss = self.get_xd_zss()
        if xds and xd_zss:
            self._xd_bsp.calculate(xds, xd_zss)

        return self
