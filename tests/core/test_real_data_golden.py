"""真实 parquet golden — 审计 D4-CRIT-1 剩余(真实浮点边界对拍)+ D4-HIGH-1 perf 安全前提。

锁死 HEAD 在真实市场数据(SH.600519/QQQ.US/SZ.002299 多周期, 含笔对 ~4e-16 浮点敏感性)上的
缠论信号输出(bis/xds/bi_zss/xd_zss/背驰类买卖点 结构签名)的指纹。任何改动——尤其 perf 重构
zs 中枢计算——若改变信号, golden 指纹失配报错。这是"perf 重构不改信号"的验证网(对拍网只保
inc==batch, 不保'重构前后信号不变', 故需此 golden)。

生成基线: `GEN_GOLDEN=1 PYTHONPATH=src poetry run pytest tests/core/test_real_data_golden.py`
(写 golden/real_data_signals.json, 提交后该测试即锁死当前信号)。
"""
import hashlib
import json
import os
import pathlib

import pandas as pd
import pytest

from chanlun.core.cl import CL

_HERE = pathlib.Path(__file__).resolve().parent
_FIX = _HERE.parents[1] / "tests" / "fixtures"
_GOLDEN = _HERE / "golden" / "real_data_signals.json"
_CFG = {"macd_ld_use_htf": True, "recursive_zs_diversity": False}

# (golden key, parquet 相对 tests/fixtures, code, frequency, 行数上限)
_FIXTURES = [
    ("SH.600519_5m", "SH.600519_5m.parquet", "SH.600519", "5m", 12072),
    ("QQQ.US_30m", "QQQ.US_30m.parquet", "QQQ.US", "30m", 5000),
    ("SZ.002299_1m", "SZ.002299_1m.parquet", "SZ.002299", "1m", 8000),
    ("SYN_strong_5m", "synthetic/SYN_strong_inclusion_5m.parquet", "SYN", "5m", 5000),
]


def _r(v):
    return round(float(v), 6) if v is not None else None


def _sig_lines(lines):
    return tuple(
        (ln.start.k.k_index, ln.end.k.k_index, ln.type, bool(ln.is_done()))
        for ln in lines
    )


def _sig_zss(zss):
    out = []
    for zs in zss:
        ls = getattr(zs, "lines", None) or []
        ki0 = ls[0].start.k.k_index if ls else None
        ki1 = ls[-1].end.k.k_index if ls else None
        out.append((ki0, ki1, _r(zs.zd), _r(zs.zg), len(ls), bool(zs.done)))
    return tuple(out)


def _sig_bsp(cd, use_xd):
    out = []
    for p in cd.get_branch_bspoints(use_xd=use_xd):
        af = getattr(p, "anchor_fx", None)
        ki = af.k.k_index if af is not None and af.k is not None else None
        out.append((str(p.bs_type), ki, p.level))
    return tuple(sorted(out, key=lambda x: (x[0], x[1] if x[1] is not None else -1, x[2] or 0)))


def _fingerprint(cd):
    parts = {
        "bis": _sig_lines(cd.get_bis()),
        "xds": _sig_lines(cd.get_xds()),
        "bi_zss": _sig_zss(cd.get_bi_zss()),
        "xd_zss": _sig_zss(cd.get_xd_zss()),
        "bsp_bi": _sig_bsp(cd, use_xd=False),
        "bsp_xd": _sig_bsp(cd, use_xd=True),
    }
    fp = {k: len(v) for k, v in parts.items()}
    fp["hash"] = hashlib.sha256(
        repr({k: parts[k] for k in sorted(parts)}).encode("utf-8")
    ).hexdigest()[:16]
    return fp


def _compute(key, rel, code, freq, max_rows):
    df = pd.read_parquet(_FIX / rel)
    df = df[["date", "open", "high", "low", "close", "volume"]].head(max_rows).reset_index(drop=True)
    cd = CL(code, freq, dict(_CFG))
    cd.process_klines(df)
    return _fingerprint(cd)


def _all():
    return {key: _compute(key, rel, code, freq, mr) for key, rel, code, freq, mr in _FIXTURES}


@pytest.mark.skipif(not _FIX.exists(), reason="tests/fixtures 缺失(parquet 未恢复)")
def test_real_data_signals_match_golden():
    computed = _all()
    if os.environ.get("GEN_GOLDEN") == "1" or not _GOLDEN.exists():
        _GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        _GOLDEN.write_text(json.dumps(computed, indent=2, ensure_ascii=False), encoding="utf-8")
        if not _GOLDEN.exists():  # 仅 GEN 模式跳过断言;正常缺失时也已生成
            pytest.skip("golden 基线已生成, 重跑以断言")
    golden = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    for key in computed:
        assert key in golden, f"golden 缺 {key}(需重生成基线)"
        assert computed[key] == golden[key], (
            f"真实数据信号 golden 失配 @ {key}\n  computed={computed[key]}\n  golden={golden[key]}\n"
            f"  → 若是有意改信号, GEN_GOLDEN=1 重生成;若是 perf 重构则它改了信号(D4-HIGH-1 红线)"
        )
