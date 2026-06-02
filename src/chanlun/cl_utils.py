import math
import copy
import time
from threading import RLock
from typing import Dict, List, Tuple, Union

import numpy as np
import pandas as pd

from chanlun import fun
from chanlun.core.cl_interface import BI, FX, ICL, LINE, MACD_INFOS, ZS, Config, Kline
from chanlun.core.macd import MACD
from chanlun.db import db
from chanlun.exchange import exchange
from chanlun.tools.log_util import LogUtil


_cl_config_cache = {}
_cl_config_cache_lock = RLock()
_cl_config_cache_ttl = 300
_cl_config_db_backoff_until = 0.0


def _shallow_config_copy(cfg: dict) -> dict:
    """对 cl_config 做"一层 dict + 容器值浅拷贝"。

    cl_config 结构：扁平标量 + 少量短 list / 单层 dict，无多层嵌套。
    比 copy.deepcopy 省去递归开销，同时仍隔离调用方对 list/dict 值的 in-place 修改。
    限制：只拷贝一层。若 dict 值内部还嵌套 list/dict（如 {"k": [1,2,3]}），
    内层容器仍会别名。当前 cl_config 不含此结构（list 值都在顶层），故安全；
    若 cl_config schema 变化需重新评估。
    """
    return {
        k: (list(v) if isinstance(v, list)
            else dict(v) if isinstance(v, dict)
            else v)
        for k, v in cfg.items()
    }


def _cl_config_cache_get(cache_key: str):
    now = time.time()
    with _cl_config_cache_lock:
        item = _cl_config_cache.get(cache_key)
        if item is None:
            return None
        if item["expire_at"] <= now:
            _cl_config_cache.pop(cache_key, None)
            return None
        return _shallow_config_copy(item["config"])


def _cl_config_cache_set(cache_key: str, config: dict):
    with _cl_config_cache_lock:
        _cl_config_cache[cache_key] = {
            "expire_at": time.time() + _cl_config_cache_ttl,
            "config": copy.deepcopy(config),
        }


def _cl_config_cache_invalidate(prefix: str = None):
    with _cl_config_cache_lock:
        if prefix is None:
            _cl_config_cache.clear()
            return
        for k in list(_cl_config_cache.keys()):
            if k.startswith(prefix):
                _cl_config_cache.pop(k, None)


def web_batch_get_cl_datas(
    market: str, code: str, klines: Dict[str, pd.DataFrame], cl_config: dict = None
) -> List[ICL]:
    """
    WEB 端批量计算并获取缠论数据。

    不走 fdb.get_web_cl_data 的 pkl 缓存：pkl 增量路径在数据源切换 / 长期运行时
    会导致旧版 XD 残留累积（xds 异常膨胀），因此 web 路径统一走进程内 LRU 缓存
    (cl_object_cache)，cache miss 时每次全量 process_klines。
    fdb.get_web_cl_data 仍保留给 notebook/回测脚本（其末尾追加增量假设不同）。

    :param market: 市场
    :param code: 计算的标的
    :param klines: 每个周期对应一个 K 线 DataFrame
    :param cl_config: 缠论配置
    :return: 缠论数据对象列表，顺序与 klines.keys 一致
    """
    # 局部 import: cl_object_cache 在 web 层, 而 cl_utils 在 src 层 — 这里用局部
    # import 避免 src 层硬依赖 web 层 (调用栈本身就来自 web, import 不会循环)
    try:
        from cl_app.services.cl_object_cache import get_or_compute_cl
        _cache_available = True
    except ImportError:
        _cache_available = False

    if _cache_available:
        cls = []
        for f, k in klines.items():
            cd = get_or_compute_cl(market, code, f, cl_config, k)
            cls.append(cd)
        return cls

    # 兜底: 非 web 环境调用 (notebook / cli) 直接全量, 不接 cache
    from chanlun.core.cl import CL
    cls = []
    for f, k in klines.items():
        cd = CL(code, f, dict(cl_config) if cl_config else {}, market=market)
        cd.process_klines(k)
        cls.append(cd)
    return cls


def cal_klines_macd_infos(start_k: Kline, end_k: Kline, cd: ICL) -> MACD_INFOS:
    """
    计算线中macd信息
    """
    infos = MACD_INFOS()

    idx = cd.get_idx()
    dea = np.array(idx["macd"]["dea"][start_k.index : end_k.index + 1])
    dif = np.array(idx["macd"]["dif"][start_k.index : end_k.index + 1])
    if len(dea) < 2 or len(dif) < 2:
        return infos
    zero = np.zeros(len(dea))

    infos.dif_up_cross_num = len(up_cross(dif, zero))
    infos.dif_down_cross_num = len(down_cross(dif, zero))
    infos.dea_up_cross_num = len(up_cross(dea, zero))
    infos.dea_down_cross_num = len(down_cross(dea, zero))
    infos.gold_cross_num = len(up_cross(dif, dea))
    infos.die_cross_num = len(down_cross(dif, dea))
    infos.last_dif = dif[-1]
    infos.last_dea = dea[-1]
    return infos


def cal_line_macd_infos(line: LINE, cd: ICL) -> MACD_INFOS:
    """
    计算线中macd信息
    """
    infos = MACD_INFOS()

    idx = cd.get_idx()
    dea = np.array(idx["macd"]["dea"][line.start.k.k_index : line.end.k.k_index + 1])
    dif = np.array(idx["macd"]["dif"][line.start.k.k_index : line.end.k.k_index + 1])
    if len(dea) < 2 or len(dif) < 2:
        return infos
    zero = np.zeros(len(dea))

    infos.dif_up_cross_num = len(up_cross(dif, zero))
    infos.dif_down_cross_num = len(down_cross(dif, zero))
    infos.dea_up_cross_num = len(up_cross(dea, zero))
    infos.dea_down_cross_num = len(down_cross(dea, zero))
    infos.gold_cross_num = len(up_cross(dif, dea))
    infos.die_cross_num = len(down_cross(dif, dea))
    infos.last_dif = dif[-1]
    infos.last_dea = dea[-1]
    return infos


def cal_macd_bis_is_bc(bis: List[BI], cd: ICL) -> Tuple[bool, bool]:
    """
    给定一组笔列表，判断其 macd 是否出现背驰，柱子高度变小，黄白线也缩小

    条件：
        1. 结束笔是向下，则需要结束笔的低点是给定笔的最低点
        2. 结束笔是向上，则需要结束笔的高点是给定笔的最高点

    只获取最后两块 红绿柱子的情况进行对比

    返回：
        bool 第一个值是 红绿柱子是否背驰
        bool 第二个值是 黄白线是否背驰
    """
    if len(bis) < 3:
        return False, False

    # 最后一笔不是最高或最低
    direction = bis[-1].type
    if direction == "up":
        bi_max_val = max([_bi.high for _bi in bis])
        if bi_max_val != bis[-1].high:
            return False, False
    else:
        bi_min_val = min([_bi.low for _bi in bis])
        if bi_min_val != bis[-1].low:
            return False, False

    macd_idx = cd.get_idx()["macd"]
    # 如果最后一笔内部没有找到 红绿柱子，则直接返回 True
    if direction == "up":
        macd_up_hist_max_val = max(
            macd_idx["hist"][bis[-1].start.k.k_index : bis[-1].end.k.k_index + 1]
        )
        if macd_up_hist_max_val <= 0:
            return True, True
    elif direction == "down":
        macd_down_hist_min_val = min(
            macd_idx["hist"][bis[-1].start.k.k_index : bis[-1].end.k.k_index + 1]
        )
        if macd_down_hist_min_val >= 0:
            return True, True

    # 黄白线在给定的笔区间内部，至少有一次穿越零轴
    start_k = cd.get_klines()[bis[0].start.k.k_index]
    end_k = cd.get_klines()[bis[-1].end.k.k_index]
    bis_macd_infos = cal_klines_macd_infos(start_k, end_k, cd)
    if direction == "up" and (
        bis_macd_infos.dea_up_cross_num == 0 or bis_macd_infos.dif_up_cross_num == 0
    ):
        return False, False
    if direction == "down" and (
        bis_macd_infos.dif_down_cross_num == 0 or bis_macd_infos.dif_down_cross_num == 0
    ):
        return False, False

    def get_macd_dump_info(start_fx: FX, end_fx: FX):
        # 获取给定区间内，hist dif dea 最大值，hist 出现的驼峰值列表
        start_k_index = start_fx.klines[0].k_index
        end_k_index = (
            end_fx.klines[-1].k_index
            if end_fx.klines[-1] is not None
            else end_fx.klines[1].k_index
        )
        # 根据红绿柱子的边界获取
        while True:
            if direction == "up":
                if start_k_index > 0 and macd_idx["hist"][start_k_index] > 0:
                    start_k_index -= 1
                else:
                    break
            if direction == "down":
                if start_k_index > 0 and macd_idx["hist"][start_k_index] < 0:
                    start_k_index -= 1
                else:
                    break
        while True:
            if direction == "up":
                if (
                    end_k_index < len(macd_idx["hist"])
                    and macd_idx["hist"][end_k_index] > 0
                ):
                    end_k_index += 1
                else:
                    break
            if direction == "down":
                if (
                    end_k_index < len(macd_idx["hist"])
                    and macd_idx["hist"][end_k_index] < 0
                ):
                    end_k_index += 1
                else:
                    break

        macd_hists = macd_idx["hist"][start_k_index : end_k_index + 1]
        macd_difs = macd_idx["dif"][start_k_index : end_k_index + 1]
        macd_deas = macd_idx["dea"][start_k_index : end_k_index + 1]

        max_hist = max_dif = max_dea = 0
        hist_dumps = []
        _temp_dumps = []
        for i in range(len(macd_hists)):
            _hist = macd_hists[i]
            _dif = macd_difs[i]
            _dea = macd_deas[i]
            if direction == "up" and _hist > 0:
                _temp_dumps.append(_hist)
                max_hist = max(max_hist, _hist)
            if direction == "up" and _dif > 0:
                max_dif = max(max_dif, _dif)
            if direction == "up" and _dea > 0:
                max_dea = max(max_dea, _dea)
            if direction == "down" and _hist < 0:
                _temp_dumps.append(abs(_hist))
                max_hist = max(max_hist, abs(_hist))
            if direction == "down" and _dif < 0:
                max_dif = max(max_dif, abs(_dif))
            if direction == "down" and _dea < 0:
                max_dea = max(max_dea, abs(_dea))
            if (direction == "up" and _hist < 0) or (direction == "down" and _hist > 0):
                if len(_temp_dumps) > 0:
                    hist_dumps.append(_temp_dumps)
                    _temp_dumps = []
        if len(_temp_dumps) > 0:
            hist_dumps.append(_temp_dumps)

        return max_hist, max_dif, max_dea, hist_dumps

    # 计算最后一笔的 macd 信息
    (
        last_bi_max_hist,
        last_bi_max_dif,
        last_bi_max_dea,
        last_bi_hist_dumps,
    ) = get_macd_dump_info(bis[-1].start, bis[-1].end)
    last_bi_sum_hist = sum([sum(_hists) for _hists in last_bi_hist_dumps])
    # 根据中枢数量确定比较基准段
    zss = cd.create_dn_zs("bi", bis)
    if len(zss) == 0:
        # 没有中枢，就用上一笔
        compare_start_fx = bis[-3].start
        compare_end_fx = bis[-3].end
    else:
        # 有中枢，根据最后一个中枢获取，这里也分两种情况，中枢最后一笔是否为给定笔的最后一笔
        if zss[-1].lines[-1].index == bis[-1].index:
            # 是中枢最后一笔
            compare_start_fx = zss[-1].lines[0].start
            compare_end_fx = zss[-1].lines[-2].end
        else:
            # 不是中枢最后一笔
            compare_start_fx = zss[-1].lines[0].start
            compare_end_fx = zss[-1].lines[-1].end

    (
        compare_max_hist,
        compare_max_dif,
        compare_max_dea,
        compare_hist_dumps,
    ) = get_macd_dump_info(compare_start_fx, compare_end_fx)
    compare_max_sum_hist = max([sum(_hists) for _hists in compare_hist_dumps])

    hist_bc = False
    deadif_bc = False
    if last_bi_max_hist < compare_max_hist and last_bi_sum_hist < compare_max_sum_hist:
        hist_bc = True
    if last_bi_max_dif < compare_max_dif and last_bi_max_dea < compare_max_dea:
        deadif_bc = True

    return hist_bc, deadif_bc


def cal_zs_macd_infos(zs: ZS, cd: ICL) -> MACD_INFOS:
    """
    计算中枢的macd信息
    """
    infos = MACD_INFOS()
    dea = np.array(
        cd.get_idx()["macd"]["dea"][zs.start.k.k_index : zs.end.k.k_index + 1]
    )
    dif = np.array(
        cd.get_idx()["macd"]["dif"][zs.start.k.k_index : zs.end.k.k_index + 1]
    )
    if len(dea) < 2 or len(dif) < 2:
        return infos
    zero = np.zeros(len(dea))

    infos.dif_up_cross_num = len(up_cross(dif, zero))
    infos.dif_down_cross_num = len(down_cross(dif, zero))
    infos.dea_up_cross_num = len(up_cross(dea, zero))
    infos.dea_down_cross_num = len(down_cross(dea, zero))
    infos.gold_cross_num = len(up_cross(dif, dea))
    infos.die_cross_num = len(down_cross(dif, dea))
    infos.last_dif = dif[-1]
    infos.last_dea = dea[-1]
    return infos


def query_cl_chart_config(
    market: str, code: str, suffix: str = ""
) -> Dict[str, object]:
    """
    查询指定市场和标的下的缠论和画图配置
    """
    # 期货代码去除主力合约后缀（如 KQ.M@SHFE.RB → SHFE.RB）并剔除月份数字，
    # 使不同到期月的合约共享同一份配置（如 SHFE.RB2501 和 SHFE.RB2502 都对应 SHFE.RB）。
    if market == "futures":
        code = code.upper().replace("KQ.M@", "")
        code = "".join([i for i in code if not i.isdigit()])

    local_cache_key = f"{market}:{code}:{suffix}"
    cached_config = _cl_config_cache_get(local_cache_key)
    if cached_config is not None:
        return cached_config
    # 默认配置；未在 DB 中设置时直接使用这套值
    default_config = {
        "config_use_type": "common",
        # 个人定制配置
        "kline_qk": Config.KLINE_QK_NONE.value,
        "judge_zs_qs_level": "1",
        # K线配置
        "kline_type": Config.KLINE_TYPE_DEFAULT.value,
        # 分型配置
        "fx_qy": Config.FX_QY_THREE.value,
        "fx_qj": Config.FX_QJ_K.value,
        "fx_bh": Config.FX_BH_YES.value,
        # 笔配置
        "bi_type": Config.BI_TYPE_OLD.value,
        "bi_bzh": Config.BI_BZH_YES.value,
        "bi_qj": Config.BI_QJ_DD.value,
        "bi_fx_cgd": Config.BI_FX_CHD_YES.value,
        "bi_split_k_cross_nums": "20,1",
        "fx_check_k_nums": 13,
        "allow_bi_fx_strict": "0",
        # 线段配置
        "xd_qj": Config.XD_QJ_DD.value,
        "xd_zs_max_lines_split": 11,
        "xd_allow_bi_pohuai": Config.XD_BI_POHUAI_YES.value,
        "xd_allow_split_no_highlow": "1",
        "xd_allow_split_zs_kz": "0",
        "xd_allow_split_zs_more_line": "1",
        "xd_allow_split_zs_no_direction": "1",
        # 中枢配置
        "zs_bi_type": [Config.ZS_TYPE_BZ.value],
        "zs_xd_type": [Config.ZS_TYPE_BZ.value],
        "zs_qj": Config.ZS_QJ_DD.value,
        "zs_cd": Config.ZS_CD_THREE.value,
        # 趋势判定口径：原文「趋势中前后中枢绝对不存在重叠，包括围绕中枢的
        # 瞬间波动(GG/DD)之间的重叠」(行2891/3565/3566/7795) → 用 GD（gg/dd
        # 包络不重叠才算趋势）。ZG/ZD 分离但 GG/DD 重叠，原文判为「中枢扩展」、
        # 非趋势。按原文重做，与 core CL 默认(cl.py)统一取 GD。
        "zs_wzgx": Config.ZS_WZGX_GD.value,
        "zs_optimize": "0",
        # MACD 配置（计算力度背驰）
        "idx_macd_fast": 12,
        "idx_macd_slow": 26,
        "idx_macd_signal": 9,
        # 背驰力度判断用高一周期 MACD（线段是最低级别走势类型，力度应提高
        # 一级度量：1m→5m、5m→30m…）。"1" 开 / "0" 关；关或无高周期对照时
        # 自动回退原生 MACD。纳入图表配置 → 进 cache_key，改它即触发重算。
        "macd_ld_use_htf": "1",
        # 买卖点配置
        # 两中枢及以上趋势背驰，产生一类买卖点
        "cl_mmd_cal_qs_1mmd": "1",
        # 非趋势，产生三类买卖点，后续创新高/新低且背驰，产生一类买卖点
        "cl_mmd_cal_not_qs_3mmd_1mmd": "1",
        # 趋势，产生三类买卖点，后续创新高/新低且背驰，产生一类买卖点
        "cl_mmd_cal_qs_3mmd_1mmd": "1",
        # 趋势，不创新高/新低，产生二类买卖点
        "cl_mmd_cal_qs_not_lh_2mmd": "1",
        # 趋势，新高/新低后，下一段与新高/新低段比较背驰后，产生二类买卖点
        "cl_mmd_cal_qs_bc_2mmd": "1",
        # 趋势，三类买卖点后，后续段不创新高/新低，或者有背驰，产生二类买卖点
        "cl_mmd_cal_3mmd_not_lh_bc_2mmd": "1",
        # 之前有一类买卖点，后续不创新高/新低，产生二类买卖点
        "cl_mmd_cal_1mmd_not_lh_2mmd": "1",
        # 三类买卖点后创新高/新低且不背驰，后续段不创新高/新低且背驰，产生二类买卖点
        "cl_mmd_cal_3mmd_xgxd_not_bc_2mmd": "1",
        # 回调不进入中枢的，产生三类买卖点
        "cl_mmd_cal_not_in_zs_3mmd": "1",
        # 回调不进入中枢的(中枢大于等于9段)，产生三类买卖点
        "cl_mmd_cal_not_in_zs_gt_9_3mmd": "1",
        # 缠论高级配置
        "enable_kchart_low_to_high": "0",
        # 画图默认配置
        "chart_show_infos": "0",
        "chart_show_fx": "1",
        "chart_show_bi": "1",
        "chart_show_xd": "1",
        # 缠论叠加层默认全部关闭——只显示 K线/分型/笔/线段(用户口径 2026-05-25:
        # 中枢/走势类型/5m 重做前先清场)。后端计算逻辑均保留,改这些 chart_show_*
        # 即可在图上重新打开;前端也可独立 toggle。
        "chart_show_bi_zs": "0",
        "chart_show_xd_zs": "0",
        "chart_show_bi_mmd": "0",
        "chart_show_xd_mmd": "0",
        "chart_show_bi_bc": "0",
        "chart_show_xd_bc": "0",
        "chart_show_zs_direction": "0",      # 中枢方向着色(up/down/zd)
        "chart_show_zs_expanded": "0",       # 扩展中枢加粗框
        "chart_show_xd_zslx": "0",           # 当前级别走势类型线段/区间
        "chart_show_recursive_levels": "1",  # 递归层级中枢与走势类型(重做完成,默认开显示新核心)
        "chart_use_branch_core": "1",        # 1=递归层级/买卖点用新核心(8模块,默认);0=旧链路
        "chart_show_higher_zs": "1",         # 低周期图叠加高周期线段中枢(混合多级别,默认开)
        "chart_show_interval_nest": "0",     # 区间套链 + 精确转折点
        "chart_show_ma": "0",
        "chart_show_boll": "0",
        "chart_show_futu": "macd",
        "chart_show_atr_stop_loss": False,
        "chart_show_ld": "xd",
        "chart_kline_nums": 500,
        "chart_idx_ma_period": "5,34",
        "chart_idx_vol_ma_period": "5,60",
        "chart_idx_boll_period": 20,
        "chart_idx_rsi_period": 14,
        "chart_idx_atr_period": 14,
        "chart_idx_atr_multiplier": 1.5,
        "chart_idx_cci_period": 14,
        "chart_idx_kdj_period": "9,3,3",
        "chart_qstd": "xd,0",
    }

    config = None
    now = time.time()
    should_skip_db = False
    db_consulted = False  # DB 是否被成功读取（无论命中与否）
    global _cl_config_db_backoff_until
    with _cl_config_cache_lock:
        if now < _cl_config_db_backoff_until:
            should_skip_db = True

    if not should_skip_db:
        try:
            config = db.cache_get(f"cl_config_{market}_{code}{suffix}")
            if config is None:
                config = db.cache_get(f"cl_config_{market}_common{suffix}")
            db_consulted = True
            with _cl_config_cache_lock:
                _cl_config_db_backoff_until = 0.0
        except Exception as e:
            with _cl_config_cache_lock:
                _cl_config_db_backoff_until = time.time() + 30
            LogUtil.error(
                f"[query_cl_chart_config] db cache_get failed market={market} code={code} err={e}",
                exc_info=True,
            )

    result_config = copy.deepcopy(default_config)
    if isinstance(config, dict):
        for _key, _val in config.items():
            result_config[_key] = _val

    # 只有成功读过 DB 才写本地缓存（M8）。退避期跳过 / 读失败时返回的是
    # "默认配置兜底"，若也缓存，一次 DB 抖动会把默认配置钉住
    # _cl_config_cache_ttl（300s）—— 即便 DB 已恢复，用户的自选配置仍被
    # 默认值覆盖数分钟。不缓存兜底值即可让退避到期后立刻读回真实配置。
    if db_consulted:
        _cl_config_cache_set(local_cache_key, result_config)
    return result_config


def set_cl_chart_config(
    market: str, code: str, config: Dict[str, object], suffix: str = ""
) -> bool:
    """
    设置指定市场和标的下的缠论和画图配置
    """
    # 期货代码规范化（同 query_cl_chart_config，保证 key 一致）
    if market == "futures":
        code = code.upper().replace("KQ.M@", "")
        code = "".join([i for i in code if not i.isdigit()])

    # 读取已有配置后做增量覆盖，避免遗漏未传入的字段
    old_config = query_cl_chart_config(market, code, suffix)
    if config["config_use_type"] == "custom" and code is None:
        return False
    elif config["config_use_type"] == "common":
        db.cache_del(f"cl_config_{market}_{code}{suffix}")

    for new_key, new_val in config.items():
        if new_key in old_config.keys():
            old_config[new_key] = new_val
        else:
            old_config[new_key] = new_val

    db.cache_set(
        f"cl_config_{market}_{code if config['config_use_type'] == 'custom' else 'common'}{suffix}",
        old_config,
    )
    _cl_config_cache_invalidate(f"{market}:")
    return True


def del_cl_chart_config(market: str, code: str, suffix: str = "") -> bool:
    """
    删除指定市场标的的独立配置项
    """
    # 期货代码规范化（同 query_cl_chart_config）
    if market == "futures":
        code = code.upper().replace("KQ.M@", "")
        code = "".join([i for i in code if not i.isdigit()])

    db.cache_del(f"cl_config_{market}_{code}{suffix}")
    _cl_config_cache_invalidate(f"{market}:")
    return True


def kcharts_frequency_h_l_map(
    market: str, frequency
) -> Tuple[Union[None, str], Union[None, str]]:
    """
    将原周期，转换为新的周期进行图表展示
    按照设置好的对应关系进行返回

    返回两个值，第一个是需要获取的低级别周期值，第二个是 kcharts 画图指定的 to_frequency 值
    """
    # 高级别对应的低级别关系
    market_frequencs_map = {
        "a": {
            "m": "w",
            "w": "d",
            "d": "30m",
            "120m": "15m",
            "60m": "15m",
            "30m": "5m",
            "15m": "5m",
            "5m": "1m",
        },
        "futures": {
            "w": "d",
            "d": "60m",
            "60m": "10m",
            "30m": "5m",
            "15m": "3m",
            "10m": "2m",
            "6m": "1m",
            "5m": "1m",
            "3m": "1m",
        },
        # TODO 港美股没有写周期转换的方法，先不支持呢
        # 'us': {
        #     'y': 'q', 'q': 'm', 'm': 'w', 'w': 'd', 'd': '60m', '120m': '15m',
        #     '60m': '15m', '30m': '5m', '15m': '5m', '5m': '1m',
        # },
        # 'hk': {
        #     'y': 'm', 'm': 'w', 'w': 'd', 'd': '60m', '120m': '15m', '60m': '15m',
        #     '30m': '5m', '15m': '5m', '10m': '1m', '5m': '1m',
        # },
        "currency": {
            "w": "d",
            "d": "4h",
            "4h": "30m",
            "60m": "15m",
            "30m": "5m",
            "15m": "5m",
            "10m": "2m",
            "5m": "1m",
            "3m": "1m",
        },
    }

    try:
        return market_frequencs_map[market][frequency], f"{market}:{frequency}"
    except KeyError:
        # 不支持的 market/frequency 组合：返回 (None, None) 让调用方走默认分支。
        return None, None


def cl_qstd(cd: ICL, line_type="xd", line_num: int = 5):
    """
    缠论线段的趋势通道
    基于已完成的最后 n 条线段，线段最高两个点，线段最低两个点连线，作为趋势通道线指导交易（不一定精确）
    """
    lines = cd.get_xds() if line_type == "xd" else cd.get_bis()
    qs_lines = []
    for i in range(1, len(lines)):
        xd = lines[-i]
        if xd.is_done():
            qs_lines.append(xd)
        if len(qs_lines) == line_num:
            break

    if len(qs_lines) != line_num:
        return None

    line_highs = [
        {"val": l.high, "index": l.end.k.k_index, "date": l.end.k.date}
        for l in qs_lines
        if l.type == "up"
    ]
    line_lows = [
        {"val": l.low, "index": l.end.k.k_index, "date": l.end.k.date}
        for l in qs_lines
        if l.type == "down"
    ]
    if len(line_highs) < 2 or len(line_lows) < 2:
        return None
    line_highs = sorted(line_highs, key=lambda v: v["val"], reverse=True)
    line_lows = sorted(line_lows, key=lambda v: v["val"], reverse=False)

    def xl(one, two):
        k = (one["val"] - two["val"]) / (one["index"] - two["index"])
        return k

    qstd = {
        "up": {
            "one": line_highs[0],
            "two": line_highs[1],
            "xl": xl(line_highs[0], line_highs[1]),
        },
        "down": {
            "one": line_lows[0],
            "two": line_lows[1],
            "xl": xl(line_lows[0], line_lows[1]),
        },
    }
    chart_up_start = {
        "val": line_highs[0]["val"]
        - qstd["up"]["xl"] * (line_highs[0]["index"] - qs_lines[-1].start.k.k_index),
        "index": qs_lines[-1].start.k.k_index,
        "date": qs_lines[-1].start.k.date,
    }
    chart_up_end = {
        "val": line_highs[0]["val"]
        - qstd["up"]["xl"] * (line_highs[0]["index"] - cd.get_klines()[-1].index),
        "index": cd.get_klines()[-1].index,
        "date": cd.get_klines()[-1].date,
    }
    chart_down_start = {
        "val": line_lows[0]["val"]
        - qstd["down"]["xl"] * (line_lows[0]["index"] - qs_lines[-1].start.k.k_index),
        "index": qs_lines[-1].start.k.k_index,
        "date": qs_lines[-1].start.k.date,
    }
    chart_down_end = {
        "val": line_lows[0]["val"]
        - qstd["down"]["xl"] * (line_lows[0]["index"] - cd.get_klines()[-1].index),
        "index": cd.get_klines()[-1].index,
        "date": cd.get_klines()[-1].date,
    }
    qstd["up"]["chart"] = {
        "x": [chart_up_start["date"], chart_up_end["date"]],
        "y": [chart_up_start["val"], chart_up_end["val"]],
        "index": [chart_up_start["index"], chart_up_end["index"]],
    }
    qstd["down"]["chart"] = {
        "x": [chart_down_start["date"], chart_down_end["date"]],
        "y": [chart_down_start["val"], chart_down_end["val"]],
        "index": [chart_down_start["index"], chart_down_start["index"]],
    }

    now_point = {
        "val": cd.get_klines()[-1].c,
        "index": cd.get_klines()[-1].index,
        "date": cd.get_klines()[-1].date,
    }
    qstd["up"]["now"] = (
        "up" if xl(chart_up_start, now_point) > qstd["up"]["xl"] else "down"
    )
    qstd["down"]["now"] = (
        "up" if xl(chart_down_start, now_point) > qstd["down"]["xl"] else "down"
    )

    return qstd


def prices_jiaodu(prices):
    """
    技术价格序列中，起始与终点的角度（正为上，负为下）

    弧度 = dy / dx
        dy = 终点与起点的差值
        dx = 固定位 100000
        dy 如果不足六位数，进行补位
    不同品种的标的价格有差异，这时计算的角度会有很大的不同，不利于量化，将 dy 固定，变相的将所有标的放在一个尺度进行对比
    """
    if prices[-1] == prices[0]:
        return 0
    dy = max(prices[-1], prices[0]) - min(prices[-1], prices[0])
    dx = 100000
    while True:
        dy_len = len(str(int(dy)))
        if dy_len < 6:
            dy = dy * (10 ** (6 - dy_len))
        elif dy_len > 6:
            dy = dy / (10 ** (dy_len - 6))
        else:
            break
    # 弧度
    k = math.atan2(dy, dx)
    # 弧度转角度
    j = math.degrees(k)
    return j if prices[-1] > prices[0] else -j


def zs_to_chart_dict(zs, use_envelope: bool = False) -> dict:
    """把 ZS 中枢序列化为前端图表 dict(core/web 共享,多周期叠加复用)。

    - points: 默认中枢核心区 [ZD,ZG]; use_envelope=True 用 [DD,GG] 包络
      (递归 L≥1 高级中枢 / 扩展中枢 / 多周期叠加需表达「瞬间波动」)。
    - linestyle: done→"0"(实线) / 未完成→"1"(虚线)。
    - type: 中枢方向(up/down/zd); is_expanded/sub_count: 扩展中枢标记。
    """
    hi = zs.gg if use_envelope else zs.zg
    lo = zs.dd if use_envelope else zs.zd
    return {
        "points": [
            {
                "time": fun.datetime_to_int(zs.start.end.k.date) if zs.start else fun.datetime_to_int(zs.lines[0].start.k.date),
                "price": hi,
            },
            {
                "time": fun.datetime_to_int(zs.end.start.k.date) if zs.end else fun.datetime_to_int(zs.lines[-1].end.k.date),
                "price": lo,
            },
        ],
        "linestyle": "0" if zs.done else "1",
        "type": zs.type,
        "is_expanded": bool(getattr(zs, "expanded_with", [])),
        "sub_count": len(getattr(zs, "expanded_with", []) or []),
    }


def cl_data_to_tv_chart(
    cd: ICL, config: dict, to_frequency: str = None
) -> Union[dict, None]:
    """
    将缠论数据，转换成 tv 画图的坐标数据
    """
    klines = [
        {
            "date": k.date,
            "high": k.h,
            "low": k.l,
            "open": k.o,
            "close": k.c,
            "volume": k.a,
        }
        for k in cd.get_klines()
    ]
    klines = pd.DataFrame(klines)
    if len(klines) == 0:
        return None
    klines.loc[:, "code"] = cd.get_code()
    if to_frequency is not None:
        # to_frequency 格式为 "market:frequency"，如 "a:30m"
        market = to_frequency.split(":")[0]
        frequency = to_frequency.split(":")[1]
        if market == "a":
            klines = exchange.convert_stock_kline_frequency(klines, frequency)
        elif market == "futures":
            klines = exchange.convert_futures_kline_frequency(klines, frequency)
        elif market == "currency":
            klines = exchange.convert_currency_kline_frequency(klines, frequency)
        else:
            raise Exception(f"图表周期数据转换，不支持的市场 {market}")

    kline_ts = klines["date"].map(fun.datetime_to_int).tolist()
    kline_cs = klines["close"].tolist()
    kline_os = klines["open"].tolist()
    kline_hs = klines["high"].tolist()
    kline_ls = klines["low"].tolist()
    kline_vs = klines["volume"].tolist()

    fx_data = []
    if config["chart_show_fx"] == "1":
        # 只输出"被笔确认"的有效分型：每一笔的起止端点（bi.start / bi.end）
        # 必为分型，是缠论意义上"有效"的分型；同一分型可能同时是上一笔
        # 的 end 与下一笔的 start，按时间戳去重。
        valid_fxs = {}
        for bi in cd.get_bis():
            for fx in (bi.start, bi.end):
                if fx is None:
                    continue
                ts = fun.datetime_to_int(fx.k.date)
                valid_fxs[ts] = fx
        for ts, fx in valid_fxs.items():
            fx_data.append(
                {
                    "points": [
                        {"time": ts, "price": fx.val},
                        {"time": ts, "price": fx.val},
                    ],
                    "text": fx.type,
                }
            )

    bi_chart_data = []
    if config["chart_show_bi"] == "1":
        for bi in cd.get_bis():
            bi_chart_data.append(
                {
                    "points": [
                        {
                            "time": fun.datetime_to_int(bi.start.k.date),
                            "price": bi.start.val,
                        },
                        {
                            "time": fun.datetime_to_int(bi.end.k.date),
                            "price": bi.end.val,
                        },
                    ],
                    "linestyle": "0" if bi.is_done() else "1",
                }
            )

    xd_chart_data = []
    if config["chart_show_xd"] == "1":
        for xd in cd.get_xds():
            xd_chart_data.append(
                {
                    "points": [
                        {
                            "time": fun.datetime_to_int(xd.start.k.date),
                            "price": xd.start.val,
                        },
                        {
                            "time": fun.datetime_to_int(xd.end.k.date),
                            "price": xd.end.val,
                        },
                    ],
                    "linestyle": "0" if xd.is_done() else "1",
                }
            )

    def _zs_to_chart(zs, use_envelope: bool = False) -> dict:
        return zs_to_chart_dict(zs, use_envelope)

    def _zslx_line_points(zslx) -> list:
        """走势类型的折线端点:用其真实起止分型,而不是中枢矩形边界。"""
        start = zslx.start or zslx.zss[0].lines[0].start
        end = zslx.end or zslx.zss[-1].lines[-1].end
        return [
            {"time": fun.datetime_to_int(start.k.date), "price": start.val},
            {"time": fun.datetime_to_int(end.k.date), "price": end.val},
        ]

    def _zslx_time_bounds(zslx) -> tuple:
        """走势类型矩形区间的时间边界:沿用中枢进入/离开段口径。"""
        first_zs, last_zs = zslx.zss[0], zslx.zss[-1]
        start_date = (
            first_zs.start.end.k.date
            if first_zs.start else first_zs.lines[0].start.k.date
        )
        end_date = (
            last_zs.end.start.k.date
            if last_zs.end else last_zs.lines[-1].end.k.date
        )
        return start_date, end_date

    def _zslx_meta(zslx, level: int) -> dict:
        return {
            "zslx_type": zslx.zslx_type,
            "direction": zslx.type,
            "done": bool(zslx.done),
            "linestyle": "0" if zslx.done else "1",
            "zss_count": len(zslx.zss),
            "level": level,
        }

    def _zslx_to_band_chart(zslx, level: int = 0) -> dict:
        """走势类型区间矩形:保留原有半透明背景表达。"""
        start_date, end_date = _zslx_time_bounds(zslx)
        return {
            "points": [
                {
                    "time": fun.datetime_to_int(start_date),
                    "price": max(z.gg for z in zslx.zss),
                },
                {
                    "time": fun.datetime_to_int(end_date),
                    "price": min(z.dd for z in zslx.zss),
                },
            ],
            "line_points": _zslx_line_points(zslx),
            **_zslx_meta(zslx, level),
        }

    def _zslx_to_line_chart(zslx, level: int = 0) -> dict:
        """走势类型线段:用于表达“本级走势类型作为下一级中枢构件”。"""
        return {
            "points": _zslx_line_points(zslx),
            **_zslx_meta(zslx, level),
        }

    # 笔中枢:新核心 zs_branch(bis) 观察层(get_bi_zhongshu)。前端「笔中枢」按钮(zs_bi)
    # 控制显示。不再走旧链路 get_bi_zss(清场已弃),也不门控 chart_show_bi_zs(改前端控制)。
    bi_zs_chart_data = []
    if config.get("chart_use_branch_core", "0") == "1":
        bi_zs_chart_data = [_zs_to_chart(zs) for zs in cd.get_bi_zhongshu()]
    elif config["chart_show_bi_zs"] == "1":
        for zs_type in config["zs_bi_type"]:
            for zs in cd.get_bi_zss(zs_type):
                bi_zs_chart_data.append(_zs_to_chart(zs))

    xd_zs_chart_data = []
    if config["chart_show_xd_zs"] == "1":
        for zs_type in config["zs_xd_type"]:
            for zs in cd.get_xd_zss(zs_type):
                xd_zs_chart_data.append(_zs_to_chart(zs))

    # 走势类型 (③ xd_zslx):矩形区间 + 独立线段。
    # 线段字段用于把“当前级别走势类型”作为“下一层中枢构件”直接画出来:
    # 线段(XD) → 当前周期中枢 → 当前周期走势类型线段 → 高一级中枢。
    xd_zslx_chart_data = []
    xd_zslx_line_chart_data = []
    if config.get("chart_show_xd_zslx", "1") == "1":
        for zslx in (cd.get_xd_zslx() or []):
            if not zslx.zss:
                continue
            xd_zslx_chart_data.append(_zslx_to_band_chart(zslx, level=0))
            xd_zslx_line_chart_data.append(_zslx_to_line_chart(zslx, level=0))

    # 递归层级树 (④ recursive_levels):L1+ 中枢、走势类型——多级联立可视化。
    recursive_levels_chart_data = []
    levels = []  # 供下方「中枢升级买卖点」复用(避免重复 get_recursive_levels)
    if config.get("chart_show_recursive_levels", "1") == "1":
        try:
            if config.get("chart_use_branch_core", "0") == "1":
                levels = cd.get_recursive_branch_levels() or []   # 新核心 8 模块
            else:
                levels = cd.get_recursive_levels() or []          # 旧链路(默认)
        except Exception:
            levels = []
        for lv in levels:
            if lv.level == 0 and config.get("chart_use_branch_core", "0") != "1":
                continue   # 旧链路 L0 已在 xd_zss 展示;新核心 L0=笔中枢,需在此画出
            lv_zss = [_zs_to_chart(zs, use_envelope=True) for zs in lv.zss]
            lv_zslxs = []
            lv_zslx_lines = []
            for zslx in lv.zslxs:
                if not zslx.zss:
                    continue
                lv_zslxs.append(_zslx_to_band_chart(zslx, level=lv.level))
                lv_zslx_lines.append(_zslx_to_line_chart(zslx, level=lv.level))
            recursive_levels_chart_data.append({
                "level": lv.level,
                "zss": lv_zss,
                "zslxs": lv_zslxs,
                "zslx_lines": lv_zslx_lines,
            })

    # 区间套 (interval_nest):从最高级别趋势背驰逐级下钻到 L0 的链 + 精确转折点。
    interval_nest_chart_data = None
    if config.get("chart_show_interval_nest", "1") == "1":
        try:
            inest = cd.get_interval_nest()
        except Exception:
            inest = None
        if inest is not None and inest.links:
            links_chart = []
            for link in inest.links:
                seg = link.beichi_seg
                try:
                    end_date = seg.end.k.date
                    end_val = seg.end.val
                except AttributeError:
                    continue
                links_chart.append({
                    "level": link.level,
                    "time": fun.datetime_to_int(end_date),
                    "price": end_val,
                    "is_beichi": bool(link.is_beichi),
                    "direction": seg.type,
                })
            if links_chart:
                tp = inest.turning_point
                interval_nest_chart_data = {
                    "links": links_chart,
                    "turning_point": {
                        "time": fun.datetime_to_int(tp.k.date),
                        "price": tp.val,
                    },
                    "direction": inest.direction,
                }

    bc_infos = {}
    mmd_infos = {}

    lines = {
        "bi": cd.get_bis(),
        "xd": cd.get_xds(),
    }
    line_type_map = {"bi": "笔", "xd": "段"}
    bc_type_map = {
        "bi": "BI",
        "xd": "XD",
        "pz": "PZ",
        "qs": "QS",
    }
    mmd_type_map = {
        "1buy": "1B",
        "2buy": "2B",
        "l2buy": "L2B",
        "3buy": "3B",
        "l3buy": "L3B",
        "1sell": "1S",
        "2sell": "2S",
        "l2sell": "L2S",
        "3sell": "3S",
        "l3sell": "L3S",
    }
    for line_type, ls in lines.items():
        for l in ls:
            bcs = l.line_bcs("|")
            if len(bcs) != 0 and l.end.k.date not in bc_infos.keys():
                bc_infos[l.end.k.date] = {
                    "price": l.end.val,
                    "bc_infos": {_type: [] for _type in line_type_map.keys()},
                }
            if config[f"chart_show_{line_type}_bc"] == "1":
                for bc in bcs:
                    bc_infos[l.end.k.date]["bc_infos"][line_type].append(
                        bc_type_map[bc]
                    )

            mmds = l.line_mmds("|")
            if len(mmds) != 0 and l.end.k.date not in mmd_infos.keys():
                mmd_infos[l.end.k.date] = {
                    "price": l.end.val,
                    "mmd_infos": {_type: [] for _type in line_type_map.keys()},
                }
            if config[f"chart_show_{line_type}_mmd"] == "1":
                for mmd in mmds:
                    mmd_infos[l.end.k.date]["mmd_infos"][line_type].append(
                        mmd_type_map[mmd]
                    )

    bc_chart_data = []
    bi_bc_chart_data = []
    xd_bc_chart_data = []
    for dt, bc in bc_infos.items():
        ts = fun.datetime_to_int(dt)
        # 拆分版:笔/段独立产出,前端可分别 reconcile + 独立 toggle + 不同样式
        for _type, _bcs in bc["bc_infos"].items():
            if not _bcs:
                continue
            target = bi_bc_chart_data if _type == "bi" else xd_bc_chart_data
            target.append({
                "points": {"time": ts, "price": bc["price"]},
                "text": ",".join(list(set(_bcs))),
                "level": _type,
            })
        # 合并版(向后兼容):同时间点笔/段合并到一个 text 里
        bc_text = "/".join(
            [
                f"{line_type_map[_type]}:{','.join(list(set(_bcs)))}"
                for _type, _bcs in bc["bc_infos"].items()
                if len(_bcs) > 0
            ]
        )
        if len(bc_text) > 0:
            bc_chart_data.append({
                "points": {"time": ts, "price": bc["price"]},
                "text": bc_text,
            })

    mmd_chart_data = []
    bi_mmd_chart_data = []
    xd_mmd_chart_data = []
    for dt, mmd in mmd_infos.items():
        ts = fun.datetime_to_int(dt)
        # 拆分版:笔/段买卖点独立产出,前端独立渲染
        for _type, _mmds in mmd["mmd_infos"].items():
            if not _mmds:
                continue
            target = bi_mmd_chart_data if _type == "bi" else xd_mmd_chart_data
            target.append({
                "points": {"time": ts, "price": mmd["price"]},
                "text": ",".join(list(set(_mmds))),
                "level": _type,
            })
        # 合并版(向后兼容)
        mmd_text = "/".join(
            [
                f"{line_type_map[_type]}:{','.join(list(set(_mmds)))}"
                for _type, _mmds in mmd["mmd_infos"].items()
                if len(_mmds) > 0
            ]
        )
        if len(mmd_text) > 0:
            mmd_chart_data.append({
                "points": {"time": ts, "price": mmd["price"]},
                "text": mmd_text,
            })

    # 中枢升级买卖点:在递归各升级级别(L0=线段本身、L1+=上一级走势类型)的走势单元上
    # 跑买卖点(独立桶 'L{n}',不冲基础 bi/xd),产出更高级别转折信号(第54课升级三卖等)。
    # 合并进 xd_mmds、文本加「升」前缀以便在图上识别。升级是否出现取决于数据是否具备
    # 更高级别结构——低结构标的可能升不出级别(原文-一致)。
    if config.get("chart_show_recursive_levels", "1") == "1" and levels:
        if config.get("chart_use_branch_core", "0") == "1":
            # 新核心 8 模块:get_branch_bspoints 直接产 BuySellPoint(一/二/三类全多级)
            try:
                for _p in cd.get_branch_bspoints():
                    xd_mmd_chart_data.append({
                        "points": {
                            "time": fun.datetime_to_int(_p.anchor_fx.k.date),
                            "price": _p.anchor_fx.val,
                        },
                        "text": _p.bs_type,
                        "level": "xd",
                    })
            except Exception:
                pass
        else:
            from chanlun.core.bs_point_calculator import BsPointCalculator
            for _lv in levels:
                _units = list(cd.get_xds()) if _lv.level == 0 else levels[_lv.level - 1].zslxs
                _zt = f"L{_lv.level}"
                try:
                    BsPointCalculator(cd, zs_type=_zt).calculate(_units, _lv.zss)
                except Exception:
                    continue
                for _u in _units:
                    for _m in _u.zs_type_mmds.get(_zt, []):
                        try:
                            xd_mmd_chart_data.append({
                                "points": {
                                    "time": fun.datetime_to_int(_u.end.k.date),
                                    "price": _u.end.val,
                                },
                                "text": "升" + mmd_type_map.get(_m.name, _m.name),
                                "level": "xd",
                            })
                        except Exception:
                            continue

    fx_data.sort(key=lambda v: v["points"][0]["time"], reverse=False)
    bi_chart_data.sort(key=lambda v: v["points"][0]["time"], reverse=False)
    xd_chart_data.sort(key=lambda v: v["points"][0]["time"], reverse=False)
    bi_zs_chart_data.sort(key=lambda v: v["points"][0]["time"], reverse=False)
    xd_zs_chart_data.sort(key=lambda v: v["points"][0]["time"], reverse=False)
    bc_chart_data.sort(key=lambda v: v["points"]["time"], reverse=False)
    mmd_chart_data.sort(key=lambda v: v["points"]["time"], reverse=False)
    bi_bc_chart_data.sort(key=lambda v: v["points"]["time"], reverse=False)
    xd_bc_chart_data.sort(key=lambda v: v["points"]["time"], reverse=False)
    bi_mmd_chart_data.sort(key=lambda v: v["points"]["time"], reverse=False)
    xd_mmd_chart_data.sort(key=lambda v: v["points"]["time"], reverse=False)
    xd_zslx_chart_data.sort(key=lambda v: v["points"][0]["time"], reverse=False)
    xd_zslx_line_chart_data.sort(key=lambda v: v["points"][0]["time"], reverse=False)
    # 获取 MACD 数据
    if to_frequency is not None:
        macd = MACD(
            fast_period=int(config.get("idx_macd_fast", 12)),
            slow_period=int(config.get("idx_macd_slow", 26)),
            signal_period=int(config.get("idx_macd_signal", 9)),
        )
        macd_klines = [
            Kline(
                index=i,
                date=row["date"],
                h=float(row["high"]),
                l=float(row["low"]),
                o=float(row["open"]),
                c=float(row["close"]),
                a=float(row.get("volume") or 0.0),
            )
            for i, row in enumerate(klines.to_dict("records"))
        ]
        macd.process_macd(macd_klines)
        macd_idx = macd.get_results()["macd"]
    else:
        macd_idx = cd.get_idx()['macd']

    # 精度截断到 6 位小数, 与 higher_macd 保持一致 (tv.py:1558).
    # 默认 json 序列化会输出 17 位精度浮点数, 每个数 ~19B; round(6) 后 ~9B,
    # 4 个字段累计可省 ~200KB 响应体积.
    _dif = np.round(macd_idx['dif'], 6).tolist()
    _dea = np.round(macd_idx['dea'], 6).tolist()
    _hist = np.round(macd_idx['hist'], 6).tolist()
    _area = np.round(macd_idx.get('hist_area', []), 6).tolist()

    return {
        "t": kline_ts,
        "c": kline_cs,
        "o": kline_os,
        "h": kline_hs,
        "l": kline_ls,
        "v": kline_vs,
        "macd_dif": _dif,
        "macd_dea": _dea,
        "macd_hist": _hist,
        "macd_area": _area,
        "fxs": fx_data,
        "bis": bi_chart_data,
        "xds": xd_chart_data,
        "bi_zss": bi_zs_chart_data,
        "xd_zss": xd_zs_chart_data,
        "bcs": bc_chart_data,
        "mmds": mmd_chart_data,
        # 拆分版买卖点/背驰(笔层 vs 段层):前端可独立渲染 + 独立 toggle、
        # 用不同样式区分级别(笔=小灰、段=大显眼)。``mmds``/``bcs`` 合并版保留
        # 兼容,新前端应消费 ``bi_mmds``/``xd_mmds``/``bi_bcs``/``xd_bcs``。
        "bi_mmds": bi_mmd_chart_data,
        "xd_mmds": xd_mmd_chart_data,
        "bi_bcs": bi_bc_chart_data,
        "xd_bcs": xd_bc_chart_data,
        # 新增:③ 走势类型 / ④ 递归层级 / 区间套
        "xd_zslx": xd_zslx_chart_data,
        "xd_zslx_lines": xd_zslx_line_chart_data,
        "recursive_levels": recursive_levels_chart_data,
        "interval_nest": interval_nest_chart_data,
    }


def bi_td(bi: BI, cd: ICL):
    """
    判断是否笔停顿
    """
    if bi.is_done() is False:
        return False
    next_ks = cd.get_klines()[bi.end.klines[-1].k_index + 1 :]
    if len(next_ks) == 0:
        return False
    for _nk in next_ks:
        if bi.type == "up" and _nk.c < _nk.o and _nk.c < bi.end.klines[-1].l:
            return True
        elif bi.type == "down" and _nk.c > _nk.o and _nk.c > bi.end.klines[-1].h:
            return True

    return False


def up_cross(one_list: np.array, two_list: np.array):
    """
    获取上穿信号列表
    """
    assert len(one_list) == len(two_list), "信号输入维度不相等"
    if len(one_list) < 2:
        return []
    cross = []
    for i in range(1, len(two_list)):
        if one_list[i - 1] < two_list[i - 1] and one_list[i] > two_list[i]:
            cross.append(i)
    return cross


def down_cross(one_list: np.array, two_list: np.array):
    """
    获取下穿信号列表
    """
    assert len(one_list) == len(two_list), "信号输入维度不相等"
    if len(one_list) < 2:
        return []
    cross = []
    for i in range(1, len(two_list)):
        if one_list[i - 1] > two_list[i - 1] and one_list[i] < two_list[i]:
            cross.append(i)
    return cross


def last_done_bi(cd: ICL):
    """
    获取最后一个 完成笔
    """
    bis = cd.get_bis()
    if len(bis) == 0:
        return None
    for bi in bis[::-1]:
        if bi.is_done():
            return bi
    return None


def bi_qk_num(cd: ICL, bi: BI) -> Tuple[int, int]:
    """
    获取笔的缺口数量（分别是向上跳空，向下跳空数量）
    """
    up_qk_num = 0
    down_qk_num = 0
    _ks = cd.get_src_klines()[bi.start.k.k_index : bi.end.k.k_index + 1]
    for i in range(1, len(_ks)):
        pre_k = _ks[i - 1]
        now_k = _ks[i]
        if now_k.l > pre_k.h:
            up_qk_num += 1
        elif now_k.h < pre_k.l:
            down_qk_num += 1
    return up_qk_num, down_qk_num


def klines_to_heikin_ashi_klines(ks: pd.DataFrame) -> pd.DataFrame:
    """
    将缠论数据的普通K线，转换成平均K线数据，返回格式 pd.DataFrame
    """
    cd_klines = ks.to_dict(orient="records")

    # 平均K线公式：open=(前开+前收)/2，close=(开+高+低+收)/4，high/low 取与 open/close 的极值
    mean_klines: list = []
    for i in range(len(cd_klines)):
        if i == 0:
            mean_klines.append(cd_klines[i])
            continue
        mk = mean_klines[i - 1]
        nk = cd_klines[i]
        _open = (mk["open"] + mk["close"]) / 2
        _close = (nk["open"] + nk["high"] + nk["low"] + nk["close"]) / 4
        _high = max(nk["high"], _open, _close)
        _low = min(nk["low"], _open, _close)
        _volume = nk["volume"]
        mean_klines.append(
            {
                "code": nk["code"],
                "date": nk["date"],
                "high": _high,
                "open": _open,
                "low": _low,
                "close": _close,
                "volume": _volume,
            }
        )

    df = pd.DataFrame(mean_klines)
    return df


if __name__ == "__main__":
    cl_config = query_cl_chart_config("a", "SH.000001")
    print(cl_config)
