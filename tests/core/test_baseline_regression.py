"""tests/core/test_baseline_regression.py — US-004 baseline 回归测试。

对每个合成组合断言 ``md5(cl_snapshot(cd)) == baselines.json 中的值``。
任何改变 fxs/bis/xds/bi_zss/xd_zss 端点签名的代码改动会让本测试红灯。

真实标的 baseline (tests/fixtures/klines/) 当前未提供, 测试用 skip 兜底。
若未来加入真实 K 线 CSV, 此测试会自动加载并跑回归。
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from typing import Any, Dict

import pandas as pd
import pytest

from tests.core.conftest import _generate_kline_df, DEFAULT_CL_CONFIG, cl_snapshot
from chanlun.core.cl import CL


_BASELINES_PATH = pathlib.Path(__file__).resolve().parent / "baselines.json"
_BASELINES_LOCAL_PATH = pathlib.Path(__file__).resolve().parent / "baselines.local.json"
_FIXTURES_DIR = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "klines"


def _load_baselines() -> Dict[str, Any]:
    """合并加载 baselines.json (check-in, 合成) + baselines.local.json (本地, 真实)。

    real_klines 优先取 .local.json (开发者本地 baseline); 若 .local.json 不
    存在或没有 real_klines 节, 退化为空 (real_klines 测试统一 skip)。
    """
    if not _BASELINES_PATH.exists():
        pytest.fail(f"baselines.json 不存在: {_BASELINES_PATH} —— 跑 `python -m tests.core._record_baseline` 生成")
    data = json.loads(_BASELINES_PATH.read_text(encoding="utf-8"))
    if _BASELINES_LOCAL_PATH.exists():
        try:
            local = json.loads(_BASELINES_LOCAL_PATH.read_text(encoding="utf-8"))
            if isinstance(local, dict) and isinstance(local.get("real_klines"), dict):
                data["real_klines"] = local["real_klines"]
        except Exception:
            pass
    return data


def _snapshot_md5(cd: CL) -> str:
    snap = cl_snapshot(cd)
    blob = json.dumps(snap, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.md5(blob.encode("utf-8")).hexdigest()


_BASELINES = _load_baselines()
_SYNTHETIC_CASES = list(_BASELINES.get("synthetic", {}).items())


@pytest.mark.parametrize("name,entry", _SYNTHETIC_CASES, ids=[name for name, _ in _SYNTHETIC_CASES])
def test_synthetic_baseline_md5(name: str, entry: Dict[str, Any]):
    """合成 K 线 baseline 必跑: md5 不变即算法语义不变。"""
    params = entry["params"]
    expected_md5 = entry["md5"]

    df = _generate_kline_df(**params)
    cd = CL("T", "1m", dict(DEFAULT_CL_CONFIG))
    cd.process_klines(df)
    actual_md5 = _snapshot_md5(cd)

    assert actual_md5 == expected_md5, (
        f"\nbaseline 失配 [{name}]:\n"
        f"  expected: {expected_md5}\n"
        f"  actual:   {actual_md5}\n"
        f"  params:   {params}\n"
        f"如果这是预期的算法修复, 跑 `python -m tests.core._record_baseline` 刷新基线, "
        f"并在 commit message 说明前后差异与动机。"
    )


def _discover_real_klines_fixtures():
    """扫描 tests/fixtures/klines/, 返回 (symbol_id, csv_path) 列表。"""
    if not _FIXTURES_DIR.exists():
        return []
    return sorted([(p.stem, p) for p in _FIXTURES_DIR.glob("*.csv")])


_REAL_FIXTURES = _discover_real_klines_fixtures()


@pytest.mark.skipif(
    not _REAL_FIXTURES,
    reason="tests/fixtures/klines/ 下没有真实标的 K 线 CSV. "
    "未来需启用真实 baseline 时, 把导出的 CSV 放入此目录, 并在 baselines.json 加入 real_klines 节。",
)
@pytest.mark.parametrize("symbol_id,csv_path", _REAL_FIXTURES, ids=[s for s, _ in _REAL_FIXTURES])
def test_real_kline_baseline_md5(symbol_id: str, csv_path: pathlib.Path):
    """真实标的 baseline (可选高级层): 仅在 fixtures 目录存在时运行。"""
    real_section = _BASELINES.get("real_klines", {})
    if symbol_id not in real_section:
        pytest.skip(
            f"baselines.json 中缺少 real_klines[{symbol_id!r}] 的 md5 entry. "
            f"跑 record 脚本前需先扩展 _record_baseline.py 以支持 real_klines."
        )

    expected_md5 = real_section[symbol_id]["md5"]
    df = pd.read_csv(csv_path, parse_dates=["date"])
    cd = CL("T", "1m", dict(DEFAULT_CL_CONFIG))
    cd.process_klines(df)
    actual_md5 = _snapshot_md5(cd)

    assert actual_md5 == expected_md5, (
        f"\nreal baseline 失配 [{symbol_id}]:\n"
        f"  expected: {expected_md5}\n"
        f"  actual:   {actual_md5}"
    )
