# -*- coding: utf-8 -*-
from __future__ import annotations

from enum import Enum

"""
CL_*** 配置项，可以在调用缠论计算时，通过传递 config 变量进行变更，如 config['CL_BI_FX_STRICT'] = True
"""


class Config(Enum):
    """
    缠论配置项
    """

    # K 线类型
    KLINE_TYPE_DEFAULT = "kline_default"  # 默认K线
    KLINE_TYPE_CHANLUN = "kline_chanlun"  # 包含处理后的缠论K线
    # K 线缺口定义 (个人定制，不清楚的使用默认 none 配置)
    KLINE_QK_NONE = "none"
    KLINE_QK_CK = "ck"
    # 分型配置项
    FX_QY_MIDDLE = "fx_qy_middle"  # 分型区间所算的区域，使用分型中间的k线作为分型区间
    FX_QY_THREE = "fx_qy_three"  # 分型区间所算的区域，使用分型三根缠论k线作为区间
    FX_QJ_CK = "fx_qj_ck"  # 用顶底的缠论K线，获取分型区间
    FX_QJ_K = "fx_qj_k"  # 用顶底的原始k线，获取分型区间
    FX_BH_YES = "fx_bh_yes"  # 不判断顶底关系，即接受所有关系
    FX_BH_DINGDI = "fx_bh_dingdi"  # 顶不可以在底中，但底可以在顶中
    FX_BH_DIDING = "fx_bh_diding"  # 底不可以在顶中，但顶可以在底中
    FX_BH_NO_QBH = "fx_bh_no_qbh"  # 不允许前一个分型包含后一个分型
    FX_BH_NO_HBQ = "fx_bh_no_hbq"  # 不允许后一个分型包含前一个分型
    FX_BH_NO = "fx_bh_no"  # 顶不可以在底中，底不可以在顶中
    FX_CD_NO = "fx_cd_no"  # 顶底分型不可重叠

    # 笔配置项
    BI_TYPE_OLD = "bi_type_old"  # 笔类型，使用老笔规则
    BI_TYPE_NEW = "bi_type_new"  # 笔类型，使用新笔规则
    BI_TYPE_JDB = "bi_type_jdb"  # 笔类型，简单笔
    BI_TYPE_DD = "bi_type_dd"  # 笔类型，使用顶底成笔规则
    BI_BZH_NO = "bi_bzh_no"  # 笔标准化，不进行标准化
    BI_BZH_YES = "bi_bzh_yes"  # 笔标准化，进行标准化，画在最高最低上
    BI_QJ_DD = "bi_qj_dd"  # 笔区间，使用起止的顶底点作为区间
    BI_QJ_CK = "bi_qj_ck"  # 笔区间，使用缠论K线的最高最低价作为区间
    BI_QJ_K = "bi_qj_k"  # 笔区间，使用原始K线的最高最低价作为区间
    BI_FX_CHD_YES = "bi_fx_cgd_yes"  # 笔内分型，次高低可以成笔
    BI_FX_CHD_NO = "bi_fx_cgd_no"  # 笔内分型，次高低不可以成笔

    # 线段配置项
    XD_QJ_DD = "xd_qj_dd"  # 线段区间，使用线段的顶底点作为区间
    XD_QJ_CK = "xd_qj_ck"  # 线段区间，使用线段中缠论K线的最高最低作为区间
    XD_QJ_K = "xd_qj_k"  # 线段区间，使用线段中原始K线的最高最低作为区间
    ### 笔破坏定义：线段的结束转折笔超过或低过线段的起始位置
    XD_BI_POHUAI_NO = "no"  # 线段不支持笔破坏
    XD_BI_POHUAI_YES = "yes"  # 线段支持笔破坏
    XD_BI_POHUAI_YES_QK = "yes_qk"  # 线段支持笔破坏（笔内必须有缺口）

    # 线段标准化配置项（xd 复用，原 ZSD_BZH_*）
    XD_BZH_NO = "xd_bzh_no"  # 线段不进行标准化
    XD_BZH_YES = "xd_bzh_yes"  # 线段进行标准化

    # 中枢配置项
    ZS_TYPE_BZ = "zs_type_bz"  # 计算的中枢类型，标准中枢，中枢维持的方法
    ZS_TYPE_DN = "zs_type_dn"  # 计算中枢的类型，段内中枢，形成线段内的中枢
    ZS_TYPE_FX = "zs_type_fx"  # 计算中枢的类型，方向中枢，进入与离开线的方向相反，严格的分为上涨与下跌中枢
    ZS_TYPE_FL = "zs_type_fl"  # 计算中枢的类型，分类中枢，段内中枢的优化，包括的在线段转折的中阴中枢
    ZS_QJ_DD = "zs_qj_dd"  # 中枢区间，使用线段的顶底点作为区间
    ZS_QJ_CK = "zs_qj_ck"  # 中枢区间，使用线段中缠论K线的最高最低作为区间
    ZS_QJ_K = "zs_qj_k"  # 中枢区间，使用线段中原始K线的最高最低作为区间
    ZS_CD_THREE = "zs_cd_three"  # 中枢重叠区间依据：中枢重叠区间取前三段的重叠区域
    ZS_CD_MORE = "zs_cd_more"  # 中枢重叠区间依据：中枢重叠区间取中枢所有线段的重叠区域
    ZS_WZGX_ZGD = "zs_wzgx_zgd"  # 判断两个中枢的位置关系，比较方式，zg与zd 宽松比较
    # 判断两个中枢的位置关系，比较方式，zg与dd zd与gg 较为宽松比较
    ZS_WZGX_ZGGDD = "zs_wzgx_zggdd"
    ZS_WZGX_GD = "zs_wzgx_gd"  # 判断两个中枢的位置关系，比较方式，gg与dd 严格比较

class Level(Enum):
    """走势类型级别枚举"""
    M1 = "1分钟"
    M5 = "5分钟"
    M15 = "15分钟"
    M30 = "30分钟"
    H1 = "60分钟"
    D1 = "日线"
    W1 = "周线"
    MN1 = "月线"
