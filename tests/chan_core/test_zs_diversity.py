"""Task 1: recursive_zs_diversity 开关骨架——默认 off 与不传开关完全等价。"""
import sys

import pandas as pd

sys.path.insert(0, "src")

from chanlun.core.cl import CL
from chanlun.recursive_bt.engine.engine import CL_CFG


def _levels(cfg_extra):
    cd = CL("SH.600519", "5m", {**CL_CFG, **cfg_extra})
    cd.process_klines(
        pd.read_parquet("tests/fixtures/SH.600519_5m.parquet")
    )
    return [
        (lv.level, [(z.zd, z.zg, len(z.lines)) for z in lv.zss])
        for lv in cd.get_recursive_branch_levels()
    ]


def test_flag_off_equals_current():
    """开关 off → 与不传开关完全一致（纯加法，不改行为）。"""
    assert _levels({}) == _levels({"recursive_zs_diversity": False})


# ── Task 2: _first_third_class ──────────────────────────────────────────────
from types import SimpleNamespace as NS  # noqa: E402

from chanlun.core.zs_diversity import _first_third_class  # noqa: E402


def _seg(t, sv, ev):
    return NS(
        _type=t,
        start=NS(val=float(sv)),
        end=NS(val=float(ev)),
        zs_low=min(sv, ev),
        zs_high=max(sv, ev),
    )


def test_first_third_class_3buy():
    """核心区[10,11]; 段3向上离开到11.5, 段4回试到11.2(≥ZG=11 不破回) → 三买在下标3。"""
    segs = [
        _seg("up", 10, 11),
        _seg("down", 11, 10),
        _seg("up", 10, 11),   # 0,1,2 核心
        _seg("up", 10.8, 11.5),
        _seg("down", 11.5, 11.2),  # 3 离开, 4 回试不破
    ]
    assert _first_third_class(segs, 10.0, 11.0) == 3


def test_oscillation_not_third_class():
    """段3冲到11.5但段4回试破回10.5(<ZG) → 震荡, 非三类。"""
    segs = [
        _seg("up", 10, 11),
        _seg("down", 11, 10),
        _seg("up", 10, 11),
        _seg("up", 10.8, 11.5),
        _seg("down", 11.5, 10.5),
    ]
    assert _first_third_class(segs, 10.0, 11.0) is None


def test_first_third_class_3sell():
    """离开下 end<zd, 回试 end≤zd → 三卖在下标3。"""
    segs = [
        _seg("down", 11, 10),
        _seg("up", 10, 11),
        _seg("down", 11, 10),
        _seg("down", 10.2, 9.5),
        _seg("up", 9.5, 9.8),  # 离开下, 回试9.8≤ZD=10
    ]
    assert _first_third_class(segs, 10.0, 11.0) == 3


# ── Task 3: _refine_r4 ─────────────────────────────────────────────────────
from chanlun.core.zs_diversity import _refine_r4  # noqa: E402


def _make_zs(segs, zd, zg):
    """用 SimpleNamespace 合成一个 ZS 替身（不依赖真实 ZS 构造器）。"""
    z = NS(
        lines=list(segs[:3]),   # 核心三段存 lines
        start=segs[0],
        end=segs[3] if len(segs) > 3 else None,
        zd=float(zd),
        zg=float(zg),
        done=False,
    )
    return z


def test_refine_r4_passthrough_no_third_class():
    """无中间三类点的中枢原样穿透：_refine_r4([z], [], 3) 返回 [z]，本体不变。"""
    # 构造 3 段核心 + 1 段离开，离开回试均未触发三类条件
    # segs[3]=离开向上到11.5；但无 segs[4]（回试）→ _first_third_class 找不到对
    # → i is None → passthrough
    segs = [
        _seg("up", 10, 11),
        _seg("down", 11, 10),
        _seg("up", 10, 11),   # 核心 3 段
        _seg("up", 10.8, 11.5),  # 离开（无回试段）
    ]
    z = _make_zs(segs, 10.0, 11.0)
    # 手动把离开段塞进 lines 让 _zs_segs 读取完整序列
    z.lines = list(segs[:3])
    z.end = segs[3]

    result = _refine_r4([z], [], min_lines=3)

    assert len(result) == 1
    assert result[0] is z


def test_refine_r4_truncates_at_third_class(monkeypatch):
    """monkeypatch 强制在下标3切分，ZsCalculator 尾段重扫返回空 → 首个中枢收口到下标3之前。"""
    # 构造 6 段的合成中枢序列（核心3 + 延伸3）
    segs = [
        _seg("up", 10, 11),
        _seg("down", 11, 10),
        _seg("up", 10, 11),    # 0-2: 核心
        _seg("up", 10.8, 11.5),  # 3: 三买离开
        _seg("down", 11.5, 11.2),  # 4: 回试
        _seg("up", 11.2, 12.0),    # 5: 延续
    ]
    z = NS(
        lines=list(segs[:3]),
        start=segs[0],
        end=segs[3],
        zd=10.0,
        zg=11.0,
        done=False,
    )
    # 将所有段存入 lines，end=None（让 _zs_segs 只走 lines 路径）
    z.lines = list(segs)
    z.end = None

    # monkeypatch _first_third_class → 强制返回下标 3
    monkeypatch.setattr(
        "chanlun.core.zs_diversity._first_third_class",
        lambda *a, **k: 3,
    )

    # monkeypatch ZsCalculator → 尾段重扫返回空列表
    monkeypatch.setattr(
        "chanlun.core.zs_diversity.ZsCalculator",
        lambda **k: type("X", (), {"calculate": lambda self, l: []})(),
    )

    result = _refine_r4([z], [], min_lines=3)

    # 切分后：首个精炼中枢的 lines 只含 segs[:3]（下标 3 之前）
    assert len(result) >= 1
    first = result[0]
    # 本体段数须收口：不能仍有 6 段
    assert len(first.lines) < len(segs)
    # 切点段（segs[3]）须被设为 .end
    assert first.end is segs[3]


# ── Task R3 v1: _running_overlap_groups / _refine_r3 ──────────────────────
from types import SimpleNamespace as NS  # noqa: F811 (already imported above)

from chanlun.core.zs_diversity import _running_overlap_groups, _refine_r3  # noqa: E402


def _make_seg(lo: float, hi: float, idx: int):
    """构造带有 zs_low/zs_high/start/end k_index 的段 stub。"""
    return NS(
        _type="up" if hi > lo else "down",
        zs_low=float(lo),
        zs_high=float(hi),
        start=NS(val=float(lo), k=NS(k_index=idx * 2)),
        end=NS(val=float(hi), k=NS(k_index=idx * 2 + 1)),
    )


def test_running_overlap_groups_splits_on_gap():
    """12 段逐步上漂：前 6 段互相重叠 [10,11]；段 6 起上移到 [11.5,12.5] 与前段重叠破坏。
    期望：_running_overlap_groups 至少返回 2 组，且每组段数 >= min_seg=3。"""
    # 前 6 段：区间都在 [10, 11]
    segs = [_make_seg(10.0, 11.0, i) for i in range(6)]
    # 后 6 段：上移到 [11.5, 12.5]（与 [10,11] 重叠为空，nhi=11 < nlo=11.5 → 断枢）
    segs += [_make_seg(11.5, 12.5, i + 6) for i in range(6)]

    groups = _running_overlap_groups(segs, min_seg=3)

    assert len(groups) >= 2, f"预期 >=2 组，实际 {len(groups)}"
    for g in groups:
        assert len(g) >= 3, f"每组段数应 >=3，实际 {len(g)}"


def test_running_overlap_groups_no_split_when_all_overlap():
    """6 段全部重叠 → 仅返回 1 组。"""
    segs = [_make_seg(10.0, 11.0, i) for i in range(6)]
    groups = _running_overlap_groups(segs, min_seg=3)
    assert len(groups) == 1
    assert len(groups[0]) == 6


def test_running_overlap_groups_drops_tiny_tail():
    """前 5 段重叠，最后 2 段断枢但不足 min_seg=3 → 只返回 1 组（尾组丢弃）。"""
    segs = [_make_seg(10.0, 11.0, i) for i in range(5)]
    segs += [_make_seg(12.0, 13.0, i + 5) for i in range(2)]
    groups = _running_overlap_groups(segs, min_seg=3)
    assert len(groups) == 1
    assert len(groups[0]) == 5


def _build_fake_zs(segs, zd: float, zg: float):
    """用 SimpleNamespace 构造最小 ZS 替身供 _refine_r3 消费。"""
    z = NS(
        lines=list(segs),
        end=None,
        zd=float(zd),
        zg=float(zg),
        zs_type="xd",
        level=None,
        done=True,
        _bounds_dirty=True,
    )
    return z


def test_refine_r3_breaks_drifting_zhongshu():
    """R3 v1：合成 12 段缓漂中枢（upgrade_seg=9 触发断枢），返回 >1 子中枢。
    每个子中枢段数 >= min_lines；核心区 zd < zg。"""
    # 前 6 段：[10, 11]；后 6 段：[11.5, 12.5]——running-overlap 在第 7 段断
    segs = [_make_seg(10.0, 11.0, i) for i in range(6)]
    segs += [_make_seg(11.5, 12.5, i + 6) for i in range(6)]
    z = _build_fake_zs(segs, zd=10.0, zg=11.0)

    result = _refine_r3([z], min_lines=3, upgrade_seg=9)

    assert len(result) > 1, f"12 段缓漂中枢应被断为 >1 子中枢，实际 {len(result)}"
    for sub in result:
        assert len(sub.lines) >= 3, f"子中枢段数 >=3，实际 {len(sub.lines)}"
        assert sub.zd < sub.zg, f"子中枢核心区 zd<zg，实际 zd={sub.zd} zg={sub.zg}"


def test_refine_r3_passthrough_short_zhongshu():
    """段数 < upgrade_seg 的中枢不触发 R3，原样透传。"""
    segs = [_make_seg(10.0, 11.0, i) for i in range(6)]
    z = _build_fake_zs(segs, zd=10.0, zg=11.0)

    result = _refine_r3([z], min_lines=3, upgrade_seg=9)

    assert len(result) == 1
    assert result[0] is z


def test_refine_r3_passthrough_no_split():
    """12 段全部重叠（无漂移）→ _running_overlap_groups 返回 1 组 → 原样透传。"""
    segs = [_make_seg(10.0, 11.0, i) for i in range(12)]
    z = _build_fake_zs(segs, zd=10.0, zg=11.0)

    result = _refine_r3([z], min_lines=3, upgrade_seg=9)

    assert len(result) == 1
    assert result[0] is z


def test_r4_inc_equals_batch():
    """Task 8: R4(flag on) 增量==批量——逐块喂 K 线的精炼 L0 中枢 == 一次性批量。"""
    from pathlib import Path

    import pandas as pd
    import pytest

    fix = Path(__file__).resolve().parents[1] / "fixtures" / "SZ.002299_1m.parquet"
    if not fix.exists():
        pytest.skip("002299 fixture 不存在")
    from chanlun.core.cl import CL
    from chanlun.recursive_bt.engine.engine import CL_CFG

    df = pd.read_parquet(fix).iloc[:6000]
    cfg = {**CL_CFG, "recursive_zs_diversity": True}

    def fp(cd):
        return [(round(z.zd, 4), round(z.zg, 4), len(z.lines))
                for z in cd.get_recursive_branch_levels()[0].zss]

    cb = CL("SZ.002299", "1m", dict(cfg))
    cb.process_klines(df)
    ci = CL("SZ.002299", "1m", dict(cfg))
    for k in (2000, 4000, 6000):
        ci.process_klines(df.iloc[:k])
    assert fp(cb) == fp(ci)
