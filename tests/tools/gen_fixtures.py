# -*- coding: utf-8 -*-
"""一次性 fixture 生成器。

  python tests/tools/gen_fixtures.py --pull           # 触网拉数据(需 QMT+cq),覆盖 parquet
  python tests/tools/gen_fixtures.py --update-golden   # 离线:由已提交 parquet 重生 golden
  python tests/tools/gen_fixtures.py --pull --update-golden

固定窗口 + 拉后按 [start,end] 闭区间裁剪 → 跨适配器口径一致、可复现。
直接实例化 QMT(A股)/ cq·长桥(美股)适配器,不依赖 config.EXCHANGE_* 取值。
注:QMT 30m 等 intraday 仅保留近期窗口;日线可取多年。
"""
import argparse
import sys
from pathlib import Path

import pandas as pd

_HERE = Path(__file__).resolve()
_ROOT = _HERE.parents[2]                       # 项目根
sys.path.insert(0, str(_ROOT / "src"))         # 兜底:未安装时也能 import chanlun
sys.path.insert(0, str(_HERE.parents[1] / "chan_core"))  # for `from snapshot import`

from chanlun.base import Market                          # noqa: E402
from chanlun.core.cl import CL                           # noqa: E402
from chanlun.recursive_bt.engine import CL_CFG           # noqa: E402
from snapshot import cl_snapshot, canonical_json         # noqa: E402

FIX_DIR = _HERE.parents[1] / "fixtures"
GOLD_DIR = _HERE.parents[1] / "golden"

# 固定窗口(可复现)。start/end 用完整 datetime(cq 的 str_to_datetime 要求
# '%Y-%m-%d %H:%M:%S';QMT 也接受)。code 为项目内部格式;若适配器拒绝,查
# QMT.code_to_qmt / cq._market_of_code 调整。intraday 用近期窗口(QMT 只留近期)。
FIXTURES = [
    {"key": "SH.600519_d",   "market": Market.A,  "code": "SH.600519", "freq": "d",   "start": "2023-01-01 00:00:00", "end": "2024-06-30 23:59:59"},
    {"key": "SH.600519_30m", "market": Market.A,  "code": "SH.600519", "freq": "30m", "start": "2026-03-01 00:00:00", "end": "2026-05-31 23:59:59"},
    {"key": "QQQ.US_d",      "market": Market.US, "code": "QQQ.US",     "freq": "d",   "start": "2025-01-01 00:00:00", "end": "2026-05-31 23:59:59"},
    {"key": "QQQ.US_30m",    "market": Market.US, "code": "QQQ.US",     "freq": "30m", "start": "2026-03-01 00:00:00", "end": "2026-05-31 23:59:59"},
    # 大网(~12072 bar / 809笔 / 83个3类买卖点):钉死无界历史扫描的行为,
    # 供 P3 把 _find_recent_1mmd_lines/_detect_3buy_3sell 收界后零漂移验证。
    {"key": "SH.600519_5m",  "market": Market.A,  "code": "SH.600519", "freq": "5m",  "start": "2025-06-01 00:00:00", "end": "2026-06-15 23:59:59"},
]


def _exchange_for(market):
    """直接给出 qmt(A股)/ cq·长桥(美股)适配器。"""
    if market == Market.A:
        from chanlun.exchange.exchange_qmt import ExchangeQMT
        return ExchangeQMT()
    if market == Market.US:
        from chanlun.exchange.exchange_cq import ExchangeChangQiao
        cq = ExchangeChangQiao()
        cq.default_market = Market.US.value     # 长桥多市场实例须标明当前市场
        return cq
    raise ValueError(f"未支持的 fixture market: {market}")


def _pull_one(f) -> pd.DataFrame:
    ex = _exchange_for(f["market"])
    df = ex.klines(f["code"], f["freq"], start_date=f["start"], end_date=f["end"])
    if df is None or len(df) == 0:
        raise RuntimeError("klines 取数为空,检查 QMT/cq 是否就绪、code 格式")
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    # date 可能 tz-aware(QMT=Asia/Shanghai);按列自身时区构造边界,避免
    # tz-aware vs tz-naive 比较报错。保留 tz 原样存 parquet,与生产喂 CL 一致。
    tz = getattr(df["date"].dt, "tz", None)
    lo = pd.Timestamp(f["start"], tz=tz) if tz is not None else pd.Timestamp(f["start"])
    hi = pd.Timestamp(f["end"], tz=tz) if tz is not None else pd.Timestamp(f["end"])
    df = df.loc[(df["date"] >= lo) & (df["date"] <= hi)].reset_index(drop=True)
    if len(df) == 0:
        raise RuntimeError(f"窗口 [{f['start']}, {f['end']}] 内 0 bar(该周期可能无此区间,调整窗口)")
    cols = [c for c in ("code", "date", "open", "high", "low", "close", "volume") if c in df.columns]
    return df[cols]


def pull():
    """逐 fixture 容错拉取:单个失败不阻断其余,末尾汇总。"""
    FIX_DIR.mkdir(parents=True, exist_ok=True)
    ok, fail = [], []
    for f in FIXTURES:
        try:
            df = _pull_one(f)
            df.to_parquet(FIX_DIR / f"{f['key']}.parquet", index=False)
            print(f"[pull] {f['key']}: {len(df)} bars -> {f['key']}.parquet")
            ok.append(f["key"])
        except Exception as e:
            print(f"[pull][FAIL] {f['key']}: {e}")
            fail.append(f["key"])
    print(f"[pull] done: {len(ok)} ok, {len(fail)} fail" + (f" -> {','.join(fail)}" if fail else ""))


def update_golden():
    GOLD_DIR.mkdir(parents=True, exist_ok=True)
    for f in FIXTURES:
        p = FIX_DIR / f"{f['key']}.parquet"
        if not p.exists():
            print(f"[golden] 跳过 {f['key']}(无 parquet,先 --pull)")
            continue
        df = pd.read_parquet(p)
        cd = CL(f["code"], f["freq"], dict(CL_CFG))
        cd.process_klines(df)
        (GOLD_DIR / f"{f['key']}.json").write_text(canonical_json(cl_snapshot(cd)), encoding="utf-8")
        print(f"[golden] {f['key']}: bis={len(cd.get_bis())} xds={len(cd.get_xds())} -> {f['key']}.json")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--pull", action="store_true")
    ap.add_argument("--update-golden", action="store_true")
    a = ap.parse_args()
    if not (a.pull or a.update_golden):
        ap.error("至少指定 --pull 或 --update-golden")
    if a.pull:
        pull()
    if a.update_golden:
        update_golden()
