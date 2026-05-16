# -*- coding: utf-8 -*-
import datetime
from typing import Dict, Union, List, Tuple, Any
import pandas as pd

from chanlun.core.bi_calculator import BiCalculator
# 笔/段两层中枢统一走 ZsCalculator，create_dn_zs 也用临时 ZsCalculator 实例。
from chanlun.core.cl_interface import ICL, Kline, CLKline, FX, BI, XD, ZS, Config, LINE, compare_ld_beichi
from chanlun.core.cl_kline_process import CL_Kline_Process
from chanlun.core.kline_data_processor import KlineDataProcessor
from chanlun.core.macd import MACD
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
    ):
        """
        初始化缠论分析器

        Args:
            code: 标的代码
            frequency: 分析周期
            config: 配置参数字典
            start_datetime: 开始分析时间
        """
        self.code = code
        self.frequency = frequency
        self.config = config if config else {}
        self.start_datetime = start_datetime

        # 设置默认配置
        self._init_default_config()

        # 实例化K线数据处理器
        self.kline_processor = KlineDataProcessor(self.start_datetime)
        # 实例化缠论K线处理器，用于处理包含关系
        self.cl_kline_processor = CL_Kline_Process()
        # 实例化MACD计算器
        self.macd_calculator = MACD()
        # 实例化笔计算器
        self.bi_calculator = BiCalculator(bi_mode = 'strict')
        # 实例化线段计算器
        self.xd_calculator = XdCalculator(self.config)

        self.zss_calculator = ZsCalculator()
        # 笔层中枢计算器，与 zss_calculator（线段层）独立维护。
        self.bi_zss_calculator = ZsCalculator()


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
            'zs_wzgx': Config.ZS_WZGX_ZGGDD.value,
            'cal_last_zs': True,
            'use_macd_ld': True,
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
        """
        判断中枢与指定线是否构成盘整背驰

        Args:
            zs: 中枢对象
            now_line: 当前线

        Returns:
            (是否背驰, 比较的线)
        """
        if len(zs.lines) < 2:
            return False, None

        # 找到同方向的比较线
        compare_line = None
        for line in reversed(zs.lines[:-1]):
            if line.type == now_line.type:
                compare_line = line
                break

        if not compare_line:
            return False, None

        # 力度比较
        now_ld = now_line.get_ld(self)
        compare_ld = compare_line.get_ld(self)

        is_bc = compare_ld_beichi(compare_ld, now_ld, now_line.type)

        return is_bc, compare_line

    def beichi_qs(
            self, lines: List[LINE], zss: List[ZS], now_line: LINE
    ) -> Tuple[bool, List[LINE]]:
        """
        判断指定线与之前的中枢，是否形成了趋势背驰

        Args:
            lines: 线的列表
            zss: 中枢列表
            now_line: 当前线

        Returns:
            (是否背驰, 比较的线列表)
        """
        if len(zss) < 2:
            return False, []

        # 检查最后两个中枢是否形成趋势
        last_zs = zss[-1]
        prev_zs = zss[-2]

        qs_direction = self.zss_is_qs(prev_zs, last_zs)
        if not qs_direction or qs_direction != now_line.type:
            return False, []

        # 找到进入前一个中枢的同方向线段。
        # ZS.start 是 LINE/XD/BI 对象，用 .start.k.k_index 取进入段起点 K 索引
        # 作为时间边界。链路任一环节在边界 case（首根中枢、xd 重建）下可能为
        # None，故走 _safe_line_start_k_index 避免 AttributeError。
        prev_zs_start_k_index = self._safe_line_start_k_index(prev_zs.start)
        if prev_zs_start_k_index is None:
            return False, []

        compare_lines = []
        for line in lines:
            line_end_k_index = self._safe_line_end_k_index(line)
            if line_end_k_index is None:
                continue
            if line.type == now_line.type and line_end_k_index <= prev_zs_start_k_index:
                compare_lines.append(line)

        if not compare_lines:
            return False, []

        # 取最后一个同方向线段进行比较
        compare_line = compare_lines[-1]

        # 力度比较
        now_ld = now_line.get_ld(self)
        compare_ld = compare_line.get_ld(self)

        is_bc = compare_ld_beichi(compare_ld, now_ld, now_line.type)

        return is_bc, [compare_line]

    def zss_is_qs(self, one_zs: ZS, two_zs: ZS) -> Union[str, None]:
        """
        判断两个中枢是否形成趋势

        Args:
            one_zs: 第一个中枢
            two_zs: 第二个中枢

        Returns:
            'up' 向上趋势, 'down' 向下趋势, None 无趋势
        """
        wzgx_config = self.config.get('zs_wzgx', Config.ZS_WZGX_ZGGDD.value)

        if wzgx_config == Config.ZS_WZGX_ZGD.value:
            # 宽松比较：zg与zd
            if one_zs.zg < two_zs.zd:
                return 'up'
            elif one_zs.zd > two_zs.zg:
                return 'down'
        elif wzgx_config == Config.ZS_WZGX_ZGGDD.value:
            # 较为宽松：zg与dd, zd与gg
            if one_zs.zg < two_zs.dd:
                return 'up'
            elif one_zs.zd > two_zs.gg:
                return 'down'
        elif wzgx_config == Config.ZS_WZGX_GD.value:
            # 严格比较：gg与dd
            if one_zs.gg < two_zs.dd:
                return 'up'
            elif one_zs.dd > two_zs.gg:
                return 'down'

        return None

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
    def _safe_line_start_k_index(line) -> Union[int, None]:
        """安全取 line.start.k.k_index，任一环节为 None 返回 None。

        缠论对象链 line.start (FX) → .k (CLKline) → .k_index 在首根中枢/
        异常构造下可能缺失，故逐层兜底。
        """
        try:
            start = getattr(line, 'start', None)
            if start is None:
                return None
            k = getattr(start, 'k', None)
            if k is None:
                return None
            return getattr(k, 'k_index', None)
        except AttributeError:
            return None

    @staticmethod
    def _safe_line_end_k_index(line) -> Union[int, None]:
        """安全取 line.end.k.k_index，任一环节为 None 返回 None。"""
        try:
            end = getattr(line, 'end', None)
            if end is None:
                return None
            k = getattr(end, 'k', None)
            if k is None:
                return None
            return getattr(k, 'k_index', None)
        except AttributeError:
            return None

    @staticmethod
    def _calc_layer_sig(lines) -> tuple:
        """构造单一 line 层（xds 或 bis）的尾部签名，供 xd / bi 两层共用。

        签名 (长度, 末段 end.k.k_index, 末段 done) 任一项变化都意味着
        BsPointCalculator 需要重新扫描：长度变化=新增段，end.k.k_index
        变化=末段端点漂移，done 翻转=pending 段与 confirmed 互转。
        """
        if not lines:
            return (0, -1, False)
        last_line = lines[-1]
        end_k_idx = -1
        try:
            if last_line.end is not None and last_line.end.k is not None:
                end_k_idx = last_line.end.k.k_index
        except AttributeError:
            end_k_idx = -1
        return (
            len(lines),
            end_k_idx,
            bool(getattr(last_line, 'done', False)),
        )

    def process_mmd(self):
        """
        计算三类买卖点（1buy/1sell, 2buy/2sell, 3buy/3sell）。

        笔层（zs_type='bi'）与线段层（zs_type='xd'）对称接入 BsPointCalculator，
        共用同一识别引擎，仅输入不同（xd 层用 xds + xd 中枢，bi 层用 bis +
        bi 中枢）。get_bi_zss() / get_xd_zss() 都含 pending_zs，不丢末段买卖点。
        任一层异常由 process_klines 外层 except 统一清理，内部不单独 try。
        """
        from chanlun.core.bs_point_calculator import BsPointCalculator

        # --- 线段层 ---
        xds = self.xd_calculator.xds
        # 必须用 get_xd_zss()（含 pending_zs），zss_calculator.zss 只含已完成
        # 中枢，但趋势背驰常发生在最后一个中枢未完成时，否则末段买卖点会丢。
        xd_zss = self.get_xd_zss()
        if xds and xd_zss:
            BsPointCalculator(self, zs_type='xd').calculate(xds, xd_zss)

        # --- 笔层 ---
        # 笔层中短线信号（分钟级 1B/2B/3B）。结果挂在 BI.zs_type_mmds['bi']，
        # 与 XD.zs_type_mmds['xd'] 不冲突（两套 dict 是 LINE 实例属性）。
        bis = self.bi_calculator.bis
        bi_zss = self.get_bi_zss()
        if bis and bi_zss:
            BsPointCalculator(self, zs_type='bi').calculate(bis, bi_zss)

        return self