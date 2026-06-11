# -*- coding: utf-8 -*-
"""复现 zslx_branch 走势类型分段相邻同向违例（fix/zhongshu-l0 调查脚本）。

复现(2026-06-11, fixture=SH.000001 5m 2025-12-01~2026-06-11):
  zslx[0] up 上涨 [3851,4099] -> zslx[1] up 盘整 [3795,4197] 相邻同 _type=up。
机制(已诊断):
  _swing_segments 正确(bounds=[(0,5,up),(6,6,down),(7,9,up)]);
  _subsplit 把摆动腿内 z2..z5 高位横盘(expand链)切成盘整子段, _finalize 对盘整段
  _type 继承摆动腿方向 swing_dir=up; 而该段 end=z5 离开段终点(4197->3795 暴跌),
  段区间被拉到 [3795,4197]、标 up——「方向标签」与「段实际净位移」矛盾。
下游: _jiehe_segments(结合运算)按 _type 合并同向段 -> 真上涨与「横盘+暴跌收尾」
  并成一个 up 段 -> 30m tongjibie 三段重合的段语义失真。
违例窗口敏感性: 2025-06~2025-12 各起点均复现(同一处), 2026-02 起点不含该区段不复现。

跑法: PYTHONPATH=src;. python scripts/repro_zslx_missing_turn.py
fixture 用 parquet(浮点敏感性, csv 的 ~4e-16 噪声会漂移笔)。
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]

FIXTURE = ROOT / "tests/fixtures/klines/a_SH_000001_5m.parquet"

CL_CONFIG = {
    "zs_bi_type": ["zs_type_bz"],
    "zs_xd_type": ["zs_type_bz"],
    "idx_macd_fast": 12,
    "idx_macd_slow": 26,
    "idx_macd_signal": 9,
}


def load_klines():
    import pandas as pd
    if FIXTURE.exists():
        print(f"用现有 fixture: {FIXTURE}")
        return pd.read_parquet(FIXTURE)
    from chanlun.exchange.exchange_qmt import ExchangeQMT
    ex = ExchangeQMT()
    df = ex.klines("SH.000001", "5m", start_date="2026-01-05", end_date="2026-06-11")
    print(f"QMT 拉到 {len(df)} 根 5m K线 {df['date'].iloc[0]} ~ {df['date'].iloc[-1]}")
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(FIXTURE)
    print(f"已存 fixture: {FIXTURE}")
    return df


def main():
    df = load_klines()
    from chanlun.core.cl import CL as CoreCL  # 新核心
    cd = CoreCL("SH.000001", "5m", dict(CL_CONFIG))
    cd.process_klines(df)
    levels = cd.get_recursive_branch_levels()
    print(f"levels: {len(levels)}")
    bad_total = 0
    for lv in levels:
        zslxs = lv.zslxs
        print(f"\n== L{lv.level}: {len(lv.zss)} 中枢, {len(zslxs)} 走势类型 ==")
        for k, z in enumerate(zslxs):
            lo, hi = z.zs_low, z.zs_high
            sd = z.start.k.date if z.start is not None else None
            ed = z.end.k.date if z.end is not None else None
            print(f"  zslx[{k}] {z._type:4s} {z.zslx_type} [{lo:.1f},{hi:.1f}] "
                  f"{sd} ~ {ed} done={z.done}")
        # 违例检测: ①相邻同向(交替性破坏) ②同向且整体反向位移(漏转折的强证据)
        for k in range(1, len(zslxs)):
            a, b = zslxs[k - 1], zslxs[k]
            if a._type == b._type:
                bad_total += 1
                shift = ""
                if a._type == "up" and b.zs_high < a.zs_high and b.zs_low < a.zs_low:
                    shift = " ←整体下移!漏下跌段"
                if a._type == "down" and b.zs_high > a.zs_high and b.zs_low > a.zs_low:
                    shift = " ←整体上移!漏上涨段"
                print(f"  !! L{lv.level} zslx[{k-1}]→[{k}] 同向 {a._type}{shift}")
    print(f"\n违例总数: {bad_total}")


if __name__ == "__main__":
    main()
