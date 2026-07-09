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


def _sig_bsp_risk(cd, use_xd):
    # R3-C5: 结构止损/反弹目标风险位签名(structural_stop_below/above、rebound_target)
    out = []
    for p in cd.get_branch_bspoints(use_xd=use_xd):
        af = getattr(p, "anchor_fx", None)
        ki = af.k.k_index if af is not None and af.k is not None else None
        out.append((
            str(p.bs_type), ki, p.level,
            _r(getattr(p, "structural_stop_below", None)),
            _r(getattr(p, "structural_stop_above", None)),
            _r(getattr(p, "rebound_target", None)),
        ))
    return tuple(sorted(out, key=lambda x: (x[0], x[1] if x[1] is not None else -1, x[2] or 0)))


def _sig_bsp_pts(pts):
    out = []
    for p in (pts or []):
        af = getattr(p, "anchor_fx", None)
        ki = af.k.k_index if af is not None and getattr(af, "k", None) is not None else None
        out.append((str(getattr(p, "bs_type", "")), ki, getattr(p, "level", None)))
    return tuple(sorted(out, key=lambda x: (x[0], x[1] if x[1] is not None else -1, x[2] or 0)))


def _sig_lvl_zss(items):
    # get_branch_dingli2_upgrades 返回 [(level, ZS)]: 解包后复用 _sig_zss 并前置 level
    out = []
    for it in (items or []):
        lvl, zs = (it if isinstance(it, tuple) else (None, it))
        sig = _sig_zss([zs])
        out.append((lvl,) + (sig[0] if sig else ()))
    return tuple(out)


def _sig_bcs(bcs):
    return tuple(sorted((str(d), _r(v), str(kind)) for (d, v, kind) in (bcs or [])))


def _sig_generic(items):
    # 防御性通用签名(小转大候选/区间套 NestRead 等结构体): 类名+常见标量字段+信号段位置锚
    out = []
    for it in (items or []):
        row = [type(it).__name__]
        for fld in ("level", "bs_type", "kind", "direction"):
            row.append(str(getattr(it, fld, "")))
        seg_anchor = None
        for segf in ("signal_seg", "seg", "leave_seg"):
            seg = getattr(it, segf, None)
            if (seg is not None and getattr(seg, "start", None) is not None
                    and getattr(seg, "end", None) is not None):
                seg_anchor = (seg.start.k.k_index, seg.end.k.k_index)
                break
        row.append(seg_anchor)
        out.append(tuple(row))
    return tuple(sorted(out, key=repr))


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
    # R1-C9: 生产信号面扩展指纹——kuozhan升级级/定理二升级+三类点/类一类/背驰bcs/
    # 小转大/区间套此前零 golden 钉扎, 改坏全网恒绿。独立 hash_ext, 不动原 hash,
    # 扩面时可逐字节证明既有 6 面信号零变更。
    kuozhan = cd.get_kuozhan_levels() or []
    parts_ext = {
        "dingli2_upg_bi": _sig_lvl_zss(cd.get_branch_dingli2_upgrades(use_xd=False)),
        "dingli2_upg_xd": _sig_lvl_zss(cd.get_branch_dingli2_upgrades(use_xd=True)),
        "dingli2_bsp_bi": _sig_bsp_pts(cd.get_branch_dingli2_bspoints(use_xd=False)),
        "dingli2_bsp_xd": _sig_bsp_pts(cd.get_branch_dingli2_bspoints(use_xd=True)),
        "quasi_first_bi": _sig_bsp_pts(cd.get_branch_quasi_first(use_xd=False)),
        "quasi_first_xd": _sig_bsp_pts(cd.get_branch_quasi_first(use_xd=True)),
        "branch_bcs_bi": _sig_bcs(cd.get_branch_bcs(use_xd=False)),
        "branch_bcs_xd": _sig_bcs(cd.get_branch_bcs(use_xd=True)),
        "xiaozhuanda": _sig_generic(cd.get_xiaozhuanda_candidates()),
        "interval_nest": _sig_generic(cd.get_branch_interval_nest()),
        "kuozhan_zss": tuple(_sig_zss(l.get("zss") or []) for l in kuozhan),
        "kuozhan_bsp": tuple(_sig_bsp_pts(l.get("bsp") or []) for l in kuozhan),
        "kuozhan_bcs": tuple(_sig_bcs(l.get("bcs") or []) for l in kuozhan),
    }
    for k, v in parts_ext.items():
        fp[k] = sum(len(x) for x in v) if k.startswith("kuozhan_") else len(v)
    fp["hash_ext"] = hashlib.sha256(
        repr({k: parts_ext[k] for k in sorted(parts_ext)}).encode("utf-8")
    ).hexdigest()[:16]
    # R3-C5: 买卖点结构止损/反弹目标此前零 golden 钉扎, perf 重构或止损逻辑改动改坏这些
    # 风险位而 bs_type/k_index/level 不变时, 原 hash/hash_ext 恒绿漏网。独立 hash_stop,
    # 不动原两 hash, 重生成时可逐字节证既有 6+13 面信号零变更。
    parts_stop = {
        "bsp_risk_bi": _sig_bsp_risk(cd, use_xd=False),
        "bsp_risk_xd": _sig_bsp_risk(cd, use_xd=True),
    }
    fp["hash_stop"] = hashlib.sha256(
        repr({k: parts_stop[k] for k in sorted(parts_stop)}).encode("utf-8")
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
    if os.environ.get("GEN_GOLDEN") == "1":
        _GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        _GOLDEN.write_text(json.dumps(computed, indent=2, ensure_ascii=False), encoding="utf-8")
        pytest.skip("golden 基线已生成(GEN_GOLDEN=1), 去掉环境变量重跑以断言")
    # R12: 基线缺失必须显式失败——旧逻辑静默自再生+自比较=恒PASS假绿且污染基线,
    # 回归网在最需要它的时刻(基线丢失/新环境)失效。
    assert _GOLDEN.exists(), (
        "golden 基线缺失(tests/core/golden/real_data_signals.json): 显式失败而非静默自再生;"
        " 确认信号变更有意后用 GEN_GOLDEN=1 重生成并提交"
    )
    golden = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    for key in computed:
        assert key in golden, f"golden 缺 {key}(需重生成基线)"
        assert computed[key] == golden[key], (
            f"真实数据信号 golden 失配 @ {key}\n  computed={computed[key]}\n  golden={golden[key]}\n"
            f"  → 若是有意改信号, GEN_GOLDEN=1 重生成;若是 perf 重构则它改了信号(D4-HIGH-1 红线)"
        )
