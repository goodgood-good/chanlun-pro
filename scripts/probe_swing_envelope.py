"""R84 地基探针：3 段口径离开段污染 gg/dd → 摆动腿反转失明 的多口径对比实验。

根因（2026-06-13 第77轮 F8 暴露）：
- correct_exit(zs, min_body=3) 在 len(lines)<=3 时不剥离开段 → 3 段中枢 gg 含离开段远摆；
- body_envelope 取 lines[:3]，对 3 段中枢同样含离开段 → 扩张/升级判定也被污染；
- _swing_segments 用 z.gg/z.dd 判反转脱离，被撑爆的 gg 使脱离条件永假 → V 型转折失明。

本脚本在两个已知 fixture 上对比 5 种包络口径，验证是否存在统一解。
结论（首跑）：strip_if_leave 恢复 600519 反转可见性（1→4 腿）且不破 000001（3 腿），
但底部区域过度切分（vs 4 段基准 down/up/down）——无简单包络统一解，需 R84 系统设计
（成枢层区分本体/离开，或 3 段口径需第 4 段数据确认离开段）。

用法：.venv/Scripts/python.exe scripts/probe_swing_envelope.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

import pandas as pd  # noqa: E402

from chanlun.core.cl import CL  # noqa: E402
from chanlun.core.zs_branch import envelope  # noqa: E402
from tests.core.conftest import DEFAULT_CL_CONFIG  # noqa: E402


def swing(zss, extract):
    """参数化反转判定：extract(z)->(lo,hi)。复制 zslx_branch._swing_segments 逻辑。"""
    n = len(zss)
    if n == 0:
        return []
    if n == 1:
        return [(0, 0, None)]
    ext = [extract(z) for z in zss]
    bounds = []
    start = 0
    ei = 0
    D = "down" if (ext[1][0] + ext[1][1]) < (ext[0][0] + ext[0][1]) else "up"
    for i in range(1, n):
        lo_i, hi_i = ext[i]
        lo_e, hi_e = ext[ei]
        if D == "down":
            if lo_i < lo_e:
                ei = i
            elif lo_i > hi_e:
                bounds.append((start, ei, "down"))
                start, D = ei + 1, "up"
                ei = max(range(start, i + 1), key=lambda k: ext[k][1])
        else:
            if hi_i > hi_e:
                ei = i
            elif hi_i < lo_e:
                bounds.append((start, ei, "up"))
                start, D = ei + 1, "down"
                ei = min(range(start, i + 1), key=lambda k: ext[k][0])
    bounds.append((start, n - 1, D))
    return bounds


def e_ggdd(z):
    return (z.dd, z.gg)


def e_core(z):
    return (z.zd, z.zg)


def e_body3(z):
    return envelope(z.lines[:3]) if z.lines else (z.dd, z.gg)


def e_strip_last(z):
    if z.lines and len(z.lines) >= 3:
        return envelope(z.lines[:-1])
    return (z.dd, z.gg)


def e_strip_if_leave(z):
    """仅当末段终点远离核心区（=离开段）才剥末段。最精准的候选。"""
    if z.lines and len(z.lines) >= 3:
        last = z.lines[-1]
        if last.start is not None and last.end is not None:
            def d(v):
                return 0.0 if z.zd <= v <= z.zg else min(abs(v - z.zd), abs(v - z.zg))
            if d(last.end.val) > d(last.start.val):
                return envelope(z.lines[:-1])
    return (z.dd, z.gg)


EXTRACTORS = [
    ("gg_dd(当前)", e_ggdd),
    ("core(zd_zg)", e_core),
    ("body3", e_body3),
    ("strip_last", e_strip_last),
    ("strip_if_leave", e_strip_if_leave),
]

EXPECT = {
    "SH.600519": "V 型：down/up/down（1428→1322→1565→1250）",
    "SH.000001": "当前 gg_dd 已对：up/down/up + 30m 中枢 zd∈(3850,3900)",
}


def main() -> int:
    for code in ["SH.600519", "SH.000001"]:
        fx = ROOT / "tests" / "fixtures" / "klines" / f"a_{code.replace('.', '_')}_5m.parquet"
        if not fx.exists():
            print(f"跳过 {code}: 缺 fixture {fx}")
            continue
        df = pd.read_parquet(fx)
        cfg = dict(DEFAULT_CL_CONFIG)
        cfg["recursive_l0_min_zs_lines"] = 3
        cd = CL(code, "5m", cfg)
        cd.process_klines(df)
        lv0 = next(lv for lv in cd.get_recursive_branch_levels() if lv.level == 0)
        print(f"\n===== {code}: {len(lv0.zss)} zss (l0_min=3) =====")
        print(f"  期望：{EXPECT.get(code, '?')}")
        for name, ex in EXTRACTORS:
            b = swing(lv0.zss, ex)
            dirs = [d for _, _, d in b]
            print(f"  {name:16s}: {len(b)}腿 {dirs}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
