import copy
import time
from threading import RLock
from typing import Dict

from chanlun.core.types import Config
from chanlun.persistence.db import db
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
        # K线配置（图表默认显示包含处理后的缠论K线，便于核对合并/分型；
        # 改回 KLINE_TYPE_DEFAULT 即恢复原始K线，或在 options 设置页按标的切换）
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
        # 趋势判定口径：用 GD（gg/dd 包络均不重叠才算趋势），与 core CL 默认(cl.py)统一。
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
        # 缠论叠加层默认全部关闭——只显示 K线/分型/笔/线段。后端计算逻辑均保留,
        # 改这些 chart_show_* 即可在图上重新打开;前端也可独立 toggle。
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
