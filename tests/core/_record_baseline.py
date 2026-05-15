"""tests/core/_record_baseline.py — 重新生成 baselines.json 的脚本。

仅在以下场景手动运行:
1. 算法 bug 已修复, 旧 baseline 已失效, 需要刷新。
2. 新增了 baseline 组合 (在 SYNTHETIC_CASES 添加条目后)。

用法:
    python -m tests.core._record_baseline

会覆盖 tests/core/baselines.json 的 synthetic 节。
合成数据组合添加/删除时, 也需要同步更新 test_baseline_regression.py 的参数化列表。
"""

from __future__ import annotations

import datetime
import hashlib
import json
import pathlib
import sys
from typing import Any, Dict

# 让脚本可直接 python -m tests.core._record_baseline 运行
PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "web" / "chanlun_chart"))

from tests.core.conftest import _generate_kline_df, DEFAULT_CL_CONFIG, cl_snapshot
from chanlun.core.cl import CL


SYNTHETIC_CASES: Dict[str, Dict[str, Any]] = {
    "multi_freq.up.n500.seed42": {"n_klines": 500, "seed": 42, "trend": "up", "multi_freq": True},
    "multi_freq.down.n500.seed42": {"n_klines": 500, "seed": 42, "trend": "down", "multi_freq": True},
    "multi_freq.oscillate.n500.seed42": {"n_klines": 500, "seed": 42, "trend": "oscillate", "multi_freq": True},
    "simple.oscillate.n200.seed42": {"n_klines": 200, "seed": 42, "trend": "oscillate", "multi_freq": False},
}


def compute_snapshot_md5(cd: CL) -> str:
    snap = cl_snapshot(cd)
    blob = json.dumps(snap, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.md5(blob.encode("utf-8")).hexdigest()


def record_synthetic_baselines() -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for name, params in SYNTHETIC_CASES.items():
        df = _generate_kline_df(**params)
        cd = CL("T", "1m", dict(DEFAULT_CL_CONFIG))
        cd.process_klines(df)
        out[name] = {"md5": compute_snapshot_md5(cd), "params": params}
        print(f"  {name}: {out[name]['md5']}")
    return out


def main() -> int:
    baselines_path = pathlib.Path(__file__).resolve().parent / "baselines.json"
    print(f"Regenerating baselines into {baselines_path}")

    # 保留已有 _meta 字段中可保留的部分, 更新时间戳
    existing: Dict[str, Any] = {}
    if baselines_path.exists():
        existing = json.loads(baselines_path.read_text(encoding="utf-8"))

    meta = existing.get("_meta", {})
    meta["regenerate_cmd"] = "python -m tests.core._record_baseline"
    meta["recorded_date"] = datetime.date.today().isoformat()
    meta["schema"] = (
        "json.dumps(cl_snapshot(cd), sort_keys=True, default=str, ensure_ascii=False)"
        ".encode('utf-8') -> md5.hexdigest()"
    )
    meta.setdefault(
        "purpose",
        "缠论核心算法 cl_snapshot md5 baseline. "
        "任何改变 fxs/bis/xds/bi_zss/xd_zss 端点签名的代码改动会让 md5 失配, CI 红灯.",
    )

    new_data: Dict[str, Any] = {"_meta": meta, "synthetic": record_synthetic_baselines()}
    baselines_path.write_text(
        json.dumps(new_data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"Done. {len(new_data['synthetic'])} synthetic baselines written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
