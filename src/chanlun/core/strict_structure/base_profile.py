"""忠于缠论原义的唯一生产结构配置。

本模块固定 K 线包含、分型、笔、线段、中枢、背驰和买卖点的唯一生产定义。
价格基准等运行时事实由严格运行时工厂注入，不能在这里作为算法版本开关。
"""

from __future__ import annotations

import hashlib
import json
from typing import Dict, Union

STRICT_BASE_PROFILE_ID = "chanlun-source-faithful-base"
STRICT_STROKE_MODE = "strict-cl-k-distance"


_STRICT_BASE_CONFIG: Dict[str, Union[str, int, bool]] = {
    "strict_base_profile_id": STRICT_BASE_PROFILE_ID,
    # 先按时间顺序做方向性包含处理，再识别三根 K 线分型。
    "kline_inclusion_rule": "directional-sequential",
    "fractal_rule": "three-cl-k-both-extremes",
    "stroke_rule": STRICT_STROKE_MODE,
    # 线段采用特征序列，并明确处理缺口。
    "segment_rule": "feature-sequence",
    "segment_gap_rule": "second-feature-sequence-fractal",
    # 物理层中枢固定为进入段 + 中间三段核心 + 独立离开段，五段均须与
    # 中间三段冻结出的价格核心形成正宽度重叠。递归层仍由三个已完成的
    # 低级别走势类型构造，不把物理线段直接冒充高一级走势。
    "center_seed_rule": (
        "physical-entry-middle-three-core-independent-leave-five-overlap;"
        "recursive-three-completed-trend-types"
    ),
    "center_lifecycle_rule": "external-departure-first-outside-return-third-class",
    "center_scan_rule": "five-role-physical-seed-causal-lifecycle-owner",
    # 背驰的进入段与离开段必须同宽。若进入段是同级别连续的“进入—反向—
    # 再进入”三段，则离开段也比较三段；进入三段的第一段必须与中枢闭区间
    # 完全不重叠。面积、柱峰值和 DIF 极值任一衰减即可确认力度衰减。
    "trend_divergence_rule": (
        "entry-width-matched-one-or-three-price-extreme-any-macd-decay;"
        "single-unit-exit-three-segment-nonextending-reversal-confirmation"
    ),
    # 趋势背驰和盘整背驰都在确认时固化同级别边界，边界之后重新划分中枢；
    # 尚未形成正式中枢的长区间按三段反转证据切成连续走势，不能被后来的中枢吞并。
    "decomposition_rule": (
        "trend-or-consolidation-divergence-terminal-prefix-partition;"
        "centerless-three-segment-reversal-movement-partition"
    ),
    "first_class_rule": "trend-or-consolidation-divergence-reversal",
    # 小级别一类点可以跨级结束高一级走势。高一级二类点与普通二类点使用
    # 同一规则：一类点所在离开段之后，必须紧邻一段反向走势和第一次回抽；
    # 不再额外强制出现下一级三类点。
    "second_class_rule": (
        "same-or-lower-first-adjacent-rebound-first-pullback"
    ),
    # 力度度量同样固定；它只提供背驰证据，不能切换 K 线、分型、笔或线段定义。
    "idx_macd_fast": 12,
    "idx_macd_slow": 26,
    "idx_macd_signal": 9,
    # 所有递归结构层都读取同一物理来源的原生 MACD；层级只改变待测结构单元，
    # 不切换或估算固定高周期。每次强度测量严格裁切到该单元覆盖的来源区间。
    # 上行段面积只累计正柱，下行段只累计负柱绝对值。面积、柱峰值和 DIF
    # 是相互独立的证据；柱峰值不可用时，不能否定已经成立的面积或 DIF 衰减。
    "strict_macd_source": "same-physical-source-native-all-recursive-levels",
    "strict_macd_level_policy": "exact-unit-source-interval",
    "strict_macd_area": "same_sign_magnitude",
    "strict_macd_decay_rule": "area-or-peak-or-dif",
}


def strict_base_config() -> dict:
    """返回唯一生产结构配置的新副本。"""

    return dict(_STRICT_BASE_CONFIG)


def strict_base_config_revision() -> str:
    """返回完整生产结构配置的确定性修订标识。"""

    encoded = json.dumps(
        _STRICT_BASE_CONFIG,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
