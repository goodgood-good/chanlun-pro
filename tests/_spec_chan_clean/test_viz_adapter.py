"""C3 可视化适配器 测试：数据结构 + ★切断旧核心 import 链红线。"""

import subprocess
import sys
from pathlib import Path

from chan.types import Bar
from chan.viz_adapter import to_chart_data


def _bars():
    turns = [("D", 10, 1), ("T", 13, 5), ("D", 11, 5), ("T", 16, 5), ("D", 13, 5), ("T", 20, 5),
             ("D", 17, 5), ("T", 18, 5), ("D", 14, 5), ("T", 15, 5), ("D", 12, 5),
             ("T", 14, 5), ("D", 13, 5), ("T", 16, 5), ("D", 15, 5), ("T", 19, 5),
             ("D", 16, 5), ("T", 17, 5), ("D", 14, 5), ("T", 15, 5), ("D", 13, 5)]
    centers = [turns[0][1]]
    cur = turns[0][1]
    for _k, level, ln in turns[1:]:
        step = (level - cur) / ln
        for _ in range(ln):
            cur += step
            centers.append(round(cur, 4))
        cur = level
    return [Bar(idx=i, dt=None, o=x - 0.5, h=x + 0.5, l=x - 0.5, c=x + 0.5)
            for i, x in enumerate(centers)]


# ── 导出结构齐全、字段稳定 ──
def test_to_chart_data_shape():
    data = to_chart_data(_bars())
    assert set(data) == {"bars", "bis", "duans", "zhongshus", "mmds"}
    assert len(data["bars"]) == len(_bars())
    assert all({"idx", "o", "h", "l", "c"} <= set(b) for b in data["bars"])
    for ln in data["bis"] + data["duans"]:
        assert {"direction", "start_bar", "end_bar", "high", "low", "confirm_bar"} <= set(ln)
    for z in data["zhongshus"]:
        assert {"level", "zd", "zg", "dd", "gg"} <= set(z)


# ── ★ C3 红线：clean 包 import 不带入任何旧核心 chanlun.* 模块 ──
def test_no_old_core_import():
    src = str(Path(__file__).resolve().parents[2] / "src")
    code = (
        "import sys; import chan; import chan.viz_adapter; "
        "bad=[m for m in sys.modules if m.split('.')[0]=='chanlun']; "
        "assert not bad, bad"
    )
    r = subprocess.run([sys.executable, "-c", code], env={"PYTHONPATH": src},
                       capture_output=True, text=True)
    assert r.returncode == 0, f"clean 包带入了旧核心: {r.stdout}{r.stderr}"
