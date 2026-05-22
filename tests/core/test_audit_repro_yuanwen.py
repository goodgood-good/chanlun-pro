"""tests/core/test_audit_repro_yuanwen.py — 原著对照审查的最小复现用例（F1 / F2）。

本文件**只编码原文正确行为**，用于"确证 bug"：当前实现下应当 **失败(RED)**。
修复后应转绿(GREEN)。详见 docs/chanlun_core_audit_原著对照_2026-05-23.md。

⚠️ 这两个用例与既有测试存在**直接冲突**（既有测试锁定的是当前实现行为）：
  - F1 冲突: tests/core/test_zs_calculator.py::test_two_consecutive_zhongshu_are_both_identified
            (断言中枢1 (zg,zd)==(8,4)，即只用前两段；原文三段应为 (8,5))
  - F2 冲突: tests/core/test_beichi_calculator.py::test_beichi_qs_true
            test_beichi_calculator.py::test_beichi_qs_compare_line_is_prev_zs_entry
            (断言 A 段 == prev_zs.start；原文 A 段 == last_zs.start)
冲突需由人工裁决，本文件不修改既有测试。
"""

from __future__ import annotations

from chanlun.core import beichi_calculator as bc
from chanlun.core.cl_interface import CLKline, Config, FX, XD, ZS
from chanlun.core.zs_calculator import ZsCalculator


# ---------------------------------------------------------------------------
# 共用 fixture helper（与 test_zs_calculator / test_beichi_calculator 同款）
# ---------------------------------------------------------------------------
def _fx(kidx: int, val: float, ftype: str) -> FX:
    k = CLKline(k_index=kidx, date=None, h=val, l=val, o=val, c=val, a=0.0, klines=[])
    return FX(_type=ftype, k=k, klines=[k], val=val)


def _seg(index: int, _type: str, start_val: float, end_val: float) -> XD:
    """构造一根线段(XD)。zs_high/zs_low 按已完成段口径取端点 max/min。"""
    if _type == "up":
        start, end = _fx(index, start_val, "di"), _fx(index + 1, end_val, "ding")
    else:
        start, end = _fx(index, start_val, "ding"), _fx(index + 1, end_val, "di")
    xd = XD(start=start, end=end, _type=_type, index=index)
    xd.done = True
    xd.zs_high = max(start_val, end_val)
    xd.zs_low = min(start_val, end_val)
    return xd


def _ld(up_sum=0.0, down_sum=0.0, dif_max=0.0, dif_min=0.0) -> dict:
    return {
        "dea": {"end": 0.0, "max": 0.0, "min": 0.0},
        "dif": {"end": 0.0, "max": dif_max, "min": dif_min},
        "hist": {"sum": up_sum + down_sum, "up_sum": up_sum,
                 "down_sum": down_sum, "max": 0.0, "min": 0.0, "end": 0.0},
    }


def _zs(zg, zd, gg, dd) -> ZS:
    return ZS(zs_type="xd", start=None, zg=zg, zd=zd, gg=gg, dd=dd)


# ===========================================================================
# F1: 中枢区间 ZG/ZD 应取三段共同重叠，而非只取相邻前两段
# ===========================================================================
def test_f1_zhongshu_zd_must_account_for_third_segment():
    """原文(行2835·缠回复·严格公式)：中枢区间 = (max(a2,b2,c2), min(a1,b1,c1))
    = A,B,C 三段共同重叠；等价于第20课(行3585)同向 Z 段 ZG=min(g1,g2)/ZD=max(d1,d2)。

    构造下跌起手的四段重叠中枢(数据到此为止 → pending，避开 5 段 promotion)：
        seg0 down 100→90 : zs[90,100]
        seg1 up   90→95  : zs[90,95]
        seg2 down 95→92  : zs[92,95]   ← 低点 92 抬高真实 ZD
        seg3 up   92→94  : zs[92,94]   (第 4 段重叠，满足 min_zs_lines=4)

    原文三段重叠 → ZD = max(90,90,92) = 92, ZG = min(100,95,95) = 95。
    当前实现只用 seg0/seg1 → ZD = max(90,90) = 90（漏掉 seg2 的 92）→ 本测试 RED。
    """
    lines = [
        _seg(0, "down", 100, 90),
        _seg(1, "up", 90, 95),
        _seg(2, "down", 95, 92),
        _seg(3, "up", 92, 94),
    ]

    zss = ZsCalculator().calculate(lines)

    assert len(zss) == 1, "四段重叠应得到 1 个(pending)中枢"
    zs = zss[0]
    assert zs.zg == 95, f"ZG 应为 95(三段高点最小值)，实际 {zs.zg}"
    # —— 关键断言：原文 ZD=92，当前实现给 90 → 此行 RED 即确证 F1 ——
    assert zs.zd == 92, (
        f"原文(行2835)中枢 ZD=max(三段低点)=max(90,90,92)=92，"
        f"当前实现只用前两段得 {zs.zd}（漏算 seg2 的低点 92）"
    )


# ===========================================================================
# F2: 趋势背驰 A 段应为"连接两中枢的段"(last_zs.start)，而非 prev_zs.start
# ===========================================================================
def test_f2_trend_beichi_A_segment_is_connecting_segment():
    """原文(第24课·行5010-5015 人寿例 + 行5065-5066·9 段结构)：

    两中枢趋势 = [中枢0] → A段(连接段) → [B=中枢1] → C段。当 C 段(now_seg)离开
    末中枢 B 时：last_zs=B, prev_zs=中枢0。
      - A 段 = "连接两个中枢的一段" = 进入 B 的段 = **last_zs.start**
      - prev_zs.start = "前面低部回拉的第一段"(进入中枢0)，原文明确与 A 段**不同**

    构造向上两中枢趋势：
        prev_zs.start = seg(0) up 4→8     ← "前面低部回拉第一段"(进入中枢0)
        last_zs.start = seg(10) up 8→13   ← A 段(连接段，进入 B)
        now           = seg(20) up 13→20  ← C 段(离开 B)

    断言 beichi_qs 返回的对照段(A 段)是 last_zs.start。
    当前实现返回 prev_zs.start → 本测试 RED 即确证 F2。
    """
    prev_entry = _seg(0, "up", 4, 8)       # 前面低部回拉第一段
    connect_seg = _seg(10, "up", 8, 13)    # A 段 = 连接两中枢的段 = last_zs.start
    now = _seg(20, "up", 13, 20)           # C 段，创新高

    prev_zs = _zs(zg=9, zd=5, gg=10, dd=4)
    prev_zs.start = prev_entry
    last_zs = _zs(zg=15, zd=13, gg=16, dd=12)   # prev_zs.zg(9) < last_zs.dd(12) → 向上趋势
    last_zs.start = connect_seg

    provider = {
        (0, 1): _ld(up_sum=100, dif_max=3),    # prev_entry
        (10, 11): _ld(up_sum=80, dif_max=2),   # connect_seg (A)
        (20, 21): _ld(up_sum=40, dif_max=1),   # now (C)
    }
    ldp = lambda s, e: provider[(s.k.k_index, e.k.k_index)]

    _is_bc, compare = bc.beichi_qs(
        [], [prev_zs, last_zs], now, ldp, Config.ZS_WZGX_ZGGDD.value
    )

    # —— 关键断言：A 段应是连接段(last_zs.start)，当前实现取 prev_zs.start → RED ——
    assert compare == [connect_seg], (
        f"原文(行5065-5066)趋势背驰 A 段 = 连接两中枢的段 = last_zs.start，"
        f"当前实现取 prev_zs.start，返回 {[getattr(c, 'index', None) for c in compare]}"
    )


# ===========================================================================
# F3: 趋势判定默认档应为 GD（原文「趋势中中枢绝对不重叠，含瞬间波动 GG/DD」）
# ===========================================================================
def test_f3_default_zs_wzgx_is_gd_per_yuanwen():
    """原文(行2891/3565/3566/7795)：趋势中前后中枢「绝对不存在重叠，包括围绕
    中枢的瞬间波动(GG/DD)之间的重叠」；ZG/ZD 分离但 GG/DD 重叠 = 中枢扩展(高级别
    中枢)、**非趋势**。这正是 GD 档(gg/dd 比较)。

    用户决定按原文将默认 zs_wzgx 切为 GD。当前默认若仍是 ZGD → 本测试 RED。
    """
    from chanlun.core.cl import CL
    cd = CL("T", "1m", {})
    assert cd.config["zs_wzgx"] == Config.ZS_WZGX_GD.value, (
        f"原文趋势=GD 档；CL 默认 zs_wzgx 应为 {Config.ZS_WZGX_GD.value}，"
        f"实际 {cd.config['zs_wzgx']}"
    )


# ===========================================================================
# F6: GG/DD 应含「range 仍与 [ZD,ZG] 重叠的延伸段(含离开段)」的探出，
#     仅【整体】脱离的段不计入 —— 复核结论：当前实现已正确(表征测试,锁原文语义)
# ===========================================================================
def test_f6_gg_dd_include_overlapping_extension_segment_pokes():
    """原文(中枢中心定理一 行3587 + GG/DD 行3585/7743)：与 [ZD,ZG] range-重叠
    的段都属中枢延伸，其向上/下探出计入 GG/DD（GG=围绕中枢波动最高点 > ZG）；
    仅当某段【整体】脱离 [ZD,ZG] 时中枢结束、该段不计入。

    复核 F6 结论：当前 update_boundaries(对全部 center.lines 取 max/min) 对
    交替的 L0 段与原文等价(相邻段共享端点 → 全部段==同向 Z 段)，且 range-重叠
    的离开段探出本应计入。此为表征测试，应 GREEN(确认非缺陷、防回归)。
    """
    lines = [
        _seg(0, "up", 4, 8),
        _seg(1, "down", 8, 5),
        _seg(2, "up", 5, 9),      # 上探 9
        _seg(3, "down", 9, 3),    # 下探 3
        _seg(4, "up", 3, 12),     # 离开段：上探 12，range[3,12] 仍与 [5,8] 重叠 → 计入
        _seg(5, "down", 12, 11),  # range[11,12] 整体在 zg=8 之上 → 结束中枢，不计入
    ]
    zss = ZsCalculator().calculate(lines)
    assert len(zss) == 1
    zs = zss[0]
    assert (zs.zg, zs.zd) == (8, 5), f"中枢区间应 (8,5)，实际 ({zs.zg},{zs.zd})"
    # GG=max(各段高 8,8,9,9,12)=12（=同向 up 段高 8,9,12 的 max）；
    # DD=min(各段低 4,5,5,3,3)=3（=同向 up 段低 4,5,3 的 min）。离开段(12)计入。
    assert (zs.gg, zs.dd) == (12, 3), (
        f"GG/DD 应含延伸段(含 range-重叠离开段)探出 → (12,3)，实际 ({zs.gg},{zs.dd})"
    )
