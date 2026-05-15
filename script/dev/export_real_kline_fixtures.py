"""script/dev/export_real_kline_fixtures.py — V4 真实标的 baseline 导出工具。

从用户本地 ``chart_cache/`` 的 pickle 中反构建 K 线 → 写到
``tests/fixtures/klines/`` (本目录被 .gitignore, 不进 git), 然后计算
``cl_snapshot`` md5 写到 ``tests/core/baselines.json`` 的 ``real_klines`` 节。

完成后 ``tests/core/test_baseline_regression.py::test_real_kline_baseline_md5``
自动 enable (不再 skip), 跑真实标的回归保护。

用法 (本地开发者一次性):
    python -m script.dev.export_real_kline_fixtures
    pytest tests/core/test_baseline_regression.py -v

注意:
- 真实 K 线 CSV 含交易所/标的可识别信息, 故 .gitignore 排除, 不进 git
- baselines.json 的 ``real_klines`` md5 是开发机本地数据的 hash, 不同机器
  生成的 md5 不一样 —— 这是预期 (回归保护针对"本机历史 cd 状态稳定性",
  不针对"跨机器数据一致性")
- 若 chart_cache 为空, 脚本提示用户先在 dev server 上看几个标的预热缓存

依赖: pandas + cl_app.services.kline_recompute + tests.core.conftest.cl_snapshot
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import pickle
import re
import sys
from typing import Dict, List

import pandas as pd

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "web" / "chanlun_chart"))


# chart_cache 文件名格式: {market}_{code_with_underscore}_{frequency}_{cl_config_hash}.pkl
# 例如: a_SZ_301004_1m_5f059f60bce3a87834c08f463d1ab30e.pkl
# 反推: market=a, code=SZ.301004, frequency=1m
_CHART_CACHE_NAME = re.compile(r"^(?P<market>[a-z_]+)_(?P<code>.+)_(?P<freq>1m|5m|15m|30m|60m|120m|d|w|m)_[0-9a-f]+\.pkl$")


def _parse_chart_cache_filename(name: str):
    m = _CHART_CACHE_NAME.match(name)
    if not m:
        return None
    return m.group("market"), m.group("code"), m.group("freq")


def _load_chart_cache(path: pathlib.Path):
    """读 chart_cache pkl, 返回 chart_data dict (或 None)。"""
    try:
        with open(path, "rb") as f:
            entry = pickle.load(f)
    except Exception as e:
        print(f"  [skip] read failed: {path.name} ({e})")
        return None
    if not isinstance(entry, dict):
        return None
    return entry.get("data")


def _snapshot_md5(cd) -> str:
    from tests.core.conftest import cl_snapshot
    blob = json.dumps(cl_snapshot(cd), sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.md5(blob.encode("utf-8")).hexdigest()


def main() -> int:
    from chanlun.config import get_data_path
    from cl_app.services.kline_recompute import extract_klines_df_from_chart_data
    from tests.core.conftest import DEFAULT_CL_CONFIG
    from chanlun.core.cl import CL

    chart_cache_dir = pathlib.Path(get_data_path()) / "chart_cache"
    if not chart_cache_dir.exists():
        print(f"[ERR] chart_cache 目录不存在: {chart_cache_dir}")
        print("       先在 web dev server 上访问几个标的让 chart_cache 写入文件")
        return 1

    pkl_files = sorted(chart_cache_dir.glob("*.pkl"))
    if not pkl_files:
        print(f"[ERR] chart_cache 目录为空: {chart_cache_dir}")
        return 1

    fixtures_dir = REPO_ROOT / "tests" / "fixtures" / "klines"
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    # real_klines baseline 是机器本地数据 hash, 不进 git → 单独存到 .local.json
    baselines_local_path = REPO_ROOT / "tests" / "core" / "baselines.local.json"
    if baselines_local_path.exists():
        baselines_local = json.loads(baselines_local_path.read_text(encoding="utf-8"))
    else:
        baselines_local = {"_meta": {}, "real_klines": {}}
    real_section: Dict[str, dict] = baselines_local.setdefault("real_klines", {})

    exported: List[str] = []
    skipped: List[str] = []

    print(f"扫描 {len(pkl_files)} 个 chart_cache pkl 文件...")
    for p in pkl_files:
        parsed = _parse_chart_cache_filename(p.name)
        if parsed is None:
            skipped.append(f"{p.name} (filename pattern not matched)")
            continue
        market, code_us, freq = parsed
        code = code_us.replace("_", ".")  # 还原 code 形态 (a_SZ_301004 → SZ.301004)

        chart_data = _load_chart_cache(p)
        if chart_data is None:
            skipped.append(f"{p.name} (load failed)")
            continue
        df = extract_klines_df_from_chart_data(chart_data)
        if df.empty:
            skipped.append(f"{p.name} (empty kline df)")
            continue

        symbol_id = f"{market}_{code_us}_{freq}"
        csv_path = fixtures_dir / f"{symbol_id}.csv"
        df.to_csv(csv_path, index=False)

        # 关键: baseline md5 必须基于"CSV 读回"的 df 计算 (不是原始 df)。
        # CSV round-trip 在浮点尾位有 ~1e-15 精度损失 (近似分数无限小数),
        # 这点点差异足以让 fx/bi/xd 端点判定漂移 → md5 失配。
        # 用 read-back df 算 md5 → 测试用 read CSV → 完全一致。
        df_roundtrip = pd.read_csv(csv_path, parse_dates=["date"])
        cd = CL("T", freq, dict(DEFAULT_CL_CONFIG))
        cd.process_klines(df_roundtrip)
        md5 = _snapshot_md5(cd)

        real_section[symbol_id] = {
            "md5": md5,
            "kline_count": int(len(df)),
            "source_pkl": p.name,
            "market": market,
            "code": code,
            "frequency": freq,
        }
        exported.append(symbol_id)
        print(f"  [ok] {symbol_id}  rows={len(df)}  md5={md5[:8]}...")

    if not exported:
        print("\n[ERR] 没有 fixture 被成功导出, 检查 skipped 原因")
        for s in skipped:
            print(f"       - {s}")
        return 1

    # 写 baselines.local.json (不进 git, 见 .gitignore)
    import datetime as _dt
    baselines_local["_meta"] = {
        "purpose": (
            "real_klines baseline 是本地开发机数据的 md5, 不同机器/不同时间运行"
            "结果不同; 用于本机回归保护, 不用于跨机器一致性校验。"
        ),
        "recorded_date": _dt.date.today().isoformat(),
        "generator": "script/dev/export_real_kline_fixtures.py",
    }
    baselines_local_path.write_text(
        json.dumps(baselines_local, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(f"\n[OK] 导出 {len(exported)} 个 fixture, 跳过 {len(skipped)} 个")
    print(f"     CSV → {fixtures_dir}")
    print(f"     baseline md5 → {baselines_local_path}")
    print(f"\n下一步:")
    print(f"  pytest tests/core/test_baseline_regression.py -v")
    return 0


if __name__ == "__main__":
    sys.exit(main())
