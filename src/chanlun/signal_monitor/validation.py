# -*- coding: utf-8 -*-
"""
信号有效性验证闭环 —— 在真实行情上回放评估器，量化"信号准不准"。

做法（信号回放 + 前瞻收益）：

1. 对一个标的 + 级别梯队，沿历史按固定步长设回放点；每个回放点把各级别 K 线
   截断到该时刻、重建缠论数据，调**真实的** ``ClSignalEvaluator.evaluate``
   收信号（杜绝未来函数 —— 评估器只看截断到当下的数据）。
2. 每个信号（按 ``identity`` 去重、记首次出现）算其后 N 根 K 线的**前瞻收益**，
   按方向取号（偏多信号→上涨记为"对"）。
3. 按 分级 / 信号类型 / 方向 / 评分档 聚合：数量、胜率、平均前瞻收益。
   每条信号同时记录其**特征**（背驰强度、共振、区间套…），供重建评分用。

``run_validation_suite`` 跑多标的并把信号汇池聚合 —— 这是重建评分的可信依据。

⚠️ 前瞻收益 ≠ 真实交易盈亏（无手续费/滑点/出场规则），衡量的是信号的
**方向性预测力**，是验证"信号准不准"够用且对路的指标。

CLI：
  ``python -m chanlun.signal_monitor.validation``            单标的（fixture）
  ``python -m chanlun.signal_monitor.validation a_SZ_301004`` 指定 fixture
  ``python -m chanlun.signal_monitor.validation suite``       多标的套件（QMT 实盘 + fixture）
"""
from __future__ import annotations

import pathlib
import sys
from typing import Dict, List, Optional

import pandas as pd

from chanlun.core.cl import CL
from chanlun.signal_monitor.evaluator import ClSignalEvaluator, EvaluatorConfig

# 与 tests/core/conftest.py 的默认配置对齐
_DEFAULT_CL_CONFIG: Dict = {
    "chart_show_fx": "1", "chart_show_bi": "1", "chart_show_xd": "1",
    "chart_show_bi_zs": "1", "chart_show_xd_zs": "1",
    "chart_show_bi_mmd": "1", "chart_show_xd_mmd": "1",
    "chart_show_bi_bc": "1", "chart_show_xd_bc": "1",
    "zs_bi_type": ["zs_type_bz"], "zs_xd_type": ["zs_type_bz"],
    "idx_macd_fast": 12, "idx_macd_slow": 26, "idx_macd_signal": 9,
}

_FIXTURES_DIR = (
    pathlib.Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "klines"
)

_MIN_BARS = 60        # 某级别截断后少于此根数则跳过该级别
_DEFAULT_MAX_BARS = 2500  # 每级别只取最近这么多根，限制回放重算成本

# 套件默认 A 股篮子（流动性好、行业分散：白酒/银行/保险/成长/资源）
_DEFAULT_A_BASKET = [
    "SH.600519", "SH.601318", "SZ.000001", "SZ.300750",
    "SH.600036", "SZ.000858", "SH.601899", "SZ.002594",
]


# ---------------------------------------------------------------- 数据加载
def load_klines(csv_path) -> pd.DataFrame:
    """读 K 线 CSV（列：date,open,high,low,close,volume），date 解析为时间。"""
    return pd.read_csv(csv_path, parse_dates=["date"])


def fixture_path(symbol_key: str, level: str) -> pathlib.Path:
    """返回 tests/fixtures/klines 下的 fixture 路径，如 ('a_SZ_301004','30m')。"""
    return _FIXTURES_DIR / f"{symbol_key}_{level}.csv"


def _norm_klines(df: pd.DataFrame, max_bars: int) -> pd.DataFrame:
    """规范化 K 线 df：date 转时间、只留最近 max_bars 根、重置索引。"""
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    if max_bars and len(df) > max_bars:
        df = df.tail(max_bars)
    return df.reset_index(drop=True)


def pull_ladder_klines(
    market: str, code: str, level_ladder: List[str],
    max_bars: int = _DEFAULT_MAX_BARS,
) -> Dict[str, pd.DataFrame]:
    """用实盘交易所拉取一个标的级别梯队的 K 线；单级别失败则跳过该级别。"""
    from chanlun.exchange import Market, get_exchange

    ex = get_exchange(Market(market))
    out: Dict[str, pd.DataFrame] = {}
    for level in level_ladder:
        try:
            df = ex.klines(code, level)
            if df is not None and len(df) >= _MIN_BARS:
                out[level] = _norm_klines(df, max_bars)
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] 拉取 {market}:{code} {level} 失败: {repr(e)[:120]}")
    return out


# ---------------------------------------------------------------- 回放
def _build_cd(code: str, level: str, df: pd.DataFrame):
    """用截断后的 K 线重建某级别缠论数据；数据太少返回 None。"""
    if len(df) < _MIN_BARS:
        return None
    cd = CL(code, level, dict(_DEFAULT_CL_CONFIG))
    cd.process_klines(df.reset_index(drop=True))
    return cd


def replay_signals(
    op_klines: pd.DataFrame,
    ladder_klines: Dict[str, pd.DataFrame],
    eval_config: EvaluatorConfig,
    code: str,
    market: str,
    warmup: int = 250,
    max_checkpoints: int = 50,
) -> List[dict]:
    """沿历史回放评估器，返回按 identity 去重后的信号（含首次出现位置）。"""
    n = len(op_klines)
    if n <= warmup + 10:
        return []
    op_level = eval_config.operation_level
    evaluator = ClSignalEvaluator(market, code, code)

    stride = max(1, (n - warmup) // max(1, max_checkpoints))
    seen: Dict[str, dict] = {}

    for bar_idx in range(warmup, n, stride):
        t = op_klines["date"].iloc[bar_idx]
        cds: Dict[str, object] = {}
        for level, kl in ladder_klines.items():
            cd = _build_cd(code, level, kl[kl["date"] <= t])
            if cd is not None:
                cds[level] = cd
        if op_level not in cds:
            continue
        try:
            signals = evaluator.evaluate(cds, eval_config)
        except Exception:  # noqa: BLE001
            continue
        for sig in signals:
            if sig.identity in seen:
                continue
            fire_idx = _locate_bar(op_klines, sig.k_date, bar_idx)
            seen[sig.identity] = {
                "signal": sig, "fire_idx": fire_idx,
                "fire_date": op_klines["date"].iloc[fire_idx],
            }
    return list(seen.values())


def _locate_bar(op_klines: pd.DataFrame, k_date, fallback_idx: int) -> int:
    """按信号触发 K 时间在 op_klines 里定位下标；定位不到回退到回放点下标。"""
    if k_date is None:
        return fallback_idx
    try:
        hit = op_klines.index[op_klines["date"] == k_date]
        if len(hit) > 0:
            return int(hit[0])
    except Exception:  # noqa: BLE001
        pass
    return fallback_idx


# ---------------------------------------------------------------- 前瞻收益
def forward_return(
    op_klines: pd.DataFrame, fire_idx: int, direction: str, forward_bars: int
) -> Optional[dict]:
    """信号触发后 forward_bars 根 K 线的前瞻收益（按方向取号）。"""
    n = len(op_klines)
    if fire_idx < 0 or fire_idx >= n - 1:
        return None
    end_idx = min(fire_idx + forward_bars, n - 1)
    p0 = float(op_klines["close"].iloc[fire_idx])
    p1 = float(op_klines["close"].iloc[end_idx])
    if p0 == 0:
        return None
    ret = (p1 - p0) / p0
    signed = ret if direction == "bullish" else -ret
    return {"ret": ret, "signed_ret": signed, "hit": signed > 0}


# ---------------------------------------------------------------- 信号特征
def _signal_features(sig) -> dict:
    """从 ClSignal 抽取特征字段 —— 供重建评分时做"哪个特征真有预测力"分析。"""
    st = sig.strength
    return {
        "strength_score": (st.strength_score if st is not None else None),
        "macd_area_ratio": (st.macd_area_ratio if st is not None else None),
        "made_new_extreme": (st.made_new_extreme if st is not None else None),
        "high_aligned": sig.resonance.get("high_aligned"),
        "sub_beichi": sig.resonance.get("sub_beichi"),
        "nested": sig.interval_nesting.get("nested"),
        "line_done": sig.interval_nesting.get("line_done"),
        "zs_pos": sig.zs_context.get("price_vs_zs"),
    }


# ---------------------------------------------------------------- 聚合
def _agg(rows: List[dict]) -> dict:
    """对一组信号行算 数量/胜率/平均带号收益。"""
    if not rows:
        return {"count": 0, "win_rate": None, "avg_signed_ret": None}
    n = len(rows)
    wins = sum(1 for r in rows if r["hit"])
    avg = sum(r["signed_ret"] for r in rows) / n
    return {"count": n, "win_rate": wins / n, "avg_signed_ret": avg}


_FEATURE_KEYS = ("high_aligned", "sub_beichi", "nested", "made_new_extreme", "zs_pos")


def aggregate_rows(rows: List[dict]) -> dict:
    """按 分级/类型/方向/评分档/特征 对信号行聚合。"""
    by_grade = {g: _agg([r for r in rows if r["grade"] == g]) for g in ("A", "B", "C")}
    kinds = sorted({r["kind"] for r in rows})
    by_kind = {k: _agg([r for r in rows if r["kind"] == k]) for k in kinds}
    by_dir = {d: _agg([r for r in rows if r["direction"] == d])
              for d in ("bullish", "bearish")}
    score_buckets = ((0, 39), (40, 59), (60, 79), (80, 100))
    by_score = {f"{lo}-{hi}": _agg([r for r in rows if lo <= r["score"] <= hi])
                for lo, hi in score_buckets}
    by_feature = {}
    for fk in _FEATURE_KEYS:
        vals = sorted({str(r.get(fk)) for r in rows})
        by_feature[fk] = {v: _agg([r for r in rows if str(r.get(fk)) == v])
                          for v in vals}
    return {"overall": _agg(rows), "by_grade": by_grade, "by_kind": by_kind,
            "by_direction": by_dir, "by_score": by_score, "by_feature": by_feature}


# ---------------------------------------------------------------- 单标的验证
def run_validation(
    op_klines: pd.DataFrame,
    ladder_klines: Dict[str, pd.DataFrame],
    operation_level: str,
    level_ladder: List[str],
    code: str = "VALID",
    market: str = "a",
    forward_bars: int = 10,
    max_checkpoints: int = 50,
    warmup: int = 250,
) -> dict:
    """在真实 K 线上跑信号回放 + 前瞻收益，返回验证报告 dict。"""
    eval_config = EvaluatorConfig(
        operation_level=operation_level, level_ladder=level_ladder,
    )
    replayed = replay_signals(
        op_klines, ladder_klines, eval_config, code, market,
        warmup=warmup, max_checkpoints=max_checkpoints,
    )

    rows: List[dict] = []
    for item in replayed:
        sig = item["signal"]
        fr = forward_return(op_klines, item["fire_idx"], sig.direction, forward_bars)
        if fr is None:
            continue
        row = {
            "symbol": code, "identity": sig.identity, "kind": sig.signal_kind,
            "direction": sig.direction, "grade": sig.grade, "score": sig.score,
            "fire_date": str(item["fire_date"]),
            "ret": fr["ret"], "signed_ret": fr["signed_ret"], "hit": fr["hit"],
        }
        row.update(_signal_features(sig))
        rows.append(row)

    report = {
        "symbol": code, "market": market,
        "operation_level": operation_level, "level_ladder": level_ladder,
        "bars": len(op_klines), "forward_bars": forward_bars,
        "warmup": warmup, "max_checkpoints": max_checkpoints,
        "signals_count": len(rows),
        "signals": rows,
    }
    report.update(aggregate_rows(rows))
    return report


# ---------------------------------------------------------------- 多标的套件
def run_validation_suite(specs: List[dict], forward_bars: int = 10,
                         max_checkpoints: int = 40, warmup: int = 250) -> dict:
    """跑多标的验证并把信号汇池聚合。

    :param specs: 每项 ``{label, market, code, operation_level, level_ladder,
                  op_klines, ladder_klines}``
    :return: 含 每标的小结 + 汇池聚合 + 汇池信号明细（带特征）的报告 dict
    """
    per_symbol: List[dict] = []
    pooled: List[dict] = []
    for spec in specs:
        try:
            rep = run_validation(
                spec["op_klines"], spec["ladder_klines"],
                spec["operation_level"], spec["level_ladder"],
                code=spec["code"], market=spec.get("market", "a"),
                forward_bars=forward_bars, max_checkpoints=max_checkpoints,
                warmup=warmup,
            )
        except Exception as e:  # noqa: BLE001
            per_symbol.append({"label": spec["label"], "error": repr(e)[:140]})
            continue
        per_symbol.append({
            "label": spec["label"], "signals_count": rep["signals_count"],
            "overall": rep["overall"],
        })
        for r in rep["signals"]:
            r = dict(r)
            r["symbol"] = spec["label"]
            pooled.append(r)

    report = {
        "kind": "suite",
        "symbols": len(specs),
        "forward_bars": forward_bars, "max_checkpoints": max_checkpoints,
        "signals_count": len(pooled),
        "per_symbol": per_symbol,
        "signals": pooled,
    }
    report.update(aggregate_rows(pooled))
    return report


# ---------------------------------------------------------------- 报告
def _fmt_pct(x) -> str:
    return "  --  " if x is None else f"{x * 100:6.2f}%"


def _fmt_agg_row(label: str, a: dict) -> str:
    return (f"  {label:<14} 数量 {a['count']:>4}   "
            f"胜率 {_fmt_pct(a['win_rate'])}   "
            f"平均前瞻收益 {_fmt_pct(a['avg_signed_ret'])}")


def _agg_sections(report: dict) -> List[str]:
    """聚合各分项的可读文本块。"""
    lines = ["【总体】", _fmt_agg_row("ALL", report["overall"]),
             "【按分级】（验证核心：A 是否优于 C）"]
    for g in ("A", "B", "C"):
        lines.append(_fmt_agg_row(g + " 级", report["by_grade"][g]))
    lines.append("【按信号类型】")
    for k, a in report["by_kind"].items():
        lines.append(_fmt_agg_row(k, a))
    lines.append("【按方向】")
    for d in ("bullish", "bearish"):
        lines.append(_fmt_agg_row(d, report["by_direction"][d]))
    lines.append("【按评分档】（验证分级：前瞻收益是否随评分单调上升）")
    for k, a in report["by_score"].items():
        lines.append(_fmt_agg_row("score " + k, a))
    lines.append("【按特征】（验证各特征是否真有区分度 —— 决定要不要进评分）")
    for fk, vals in report.get("by_feature", {}).items():
        for v, a in vals.items():
            lines.append(_fmt_agg_row(f"{fk}={v}", a))
    return lines


def format_report(report: dict) -> str:
    """把单标的验证报告格式化成可读文本。"""
    lines = ["=" * 70,
             f" 信号有效性验证报告 —— {report['symbol']} "
             f"操作级别 {report['operation_level']} 梯队 {report['level_ladder']}",
             f" K线 {report['bars']} 根 | 回放点 ~{report['max_checkpoints']} | "
             f"前瞻 {report['forward_bars']} 根 | 信号 {report['signals_count']} 条",
             "=" * 70]
    lines += _agg_sections(report)
    lines.append("=" * 70)
    return "\n".join(lines)


def format_suite_report(report: dict) -> str:
    """把多标的套件报告格式化成可读文本。"""
    lines = ["#" * 70,
             f" 多标的验证套件 —— {report['symbols']} 标的 | "
             f"汇池信号 {report['signals_count']} 条 | 前瞻 {report['forward_bars']} 根",
             "#" * 70, "【各标的小结】"]
    for ps in report["per_symbol"]:
        if "error" in ps:
            lines.append(f"  {ps['label']:<14} [失败] {ps['error']}")
        else:
            o = ps["overall"]
            lines.append(f"  {ps['label']:<14} 信号 {ps['signals_count']:>3}   "
                         f"胜率 {_fmt_pct(o['win_rate'])}   "
                         f"前瞻 {_fmt_pct(o['avg_signed_ret'])}")
    lines.append("-" * 70)
    lines.append("【汇池聚合】（多标的合并 —— 重建评分的依据）")
    lines += _agg_sections(report)
    lines.append("#" * 70)
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI
def run_fixture_validation(
    symbol_key: str = "a_SZ_301004",
    operation_level: str = "30m",
    level_ladder: Optional[List[str]] = None,
    market: str = "a",
    forward_bars: int = 10,
    max_checkpoints: int = 50,
) -> dict:
    """用 tests/fixtures/klines 下的 fixture 跑一次单标的验证。"""
    level_ladder = level_ladder or ["d", "30m", "5m"]
    ladder_klines: Dict[str, pd.DataFrame] = {}
    for level in level_ladder:
        path = fixture_path(symbol_key, level)
        if not path.is_file():
            raise FileNotFoundError(f"缺少 fixture: {path}")
        ladder_klines[level] = load_klines(path)
    return run_validation(
        ladder_klines[operation_level], ladder_klines, operation_level,
        level_ladder, code=symbol_key, market=market,
        forward_bars=forward_bars, max_checkpoints=max_checkpoints,
    )


def _spec_from_live(market: str, code: str, op_level: str,
                    ladder: List[str]) -> Optional[dict]:
    """拉实盘行情组一个套件 spec；梯队数据不全则返回 None。"""
    klines = pull_ladder_klines(market, code, ladder)
    if op_level not in klines:
        return None
    return {"label": f"{market}:{code}", "market": market, "code": code,
            "operation_level": op_level, "level_ladder": ladder,
            "op_klines": klines[op_level], "ladder_klines": klines}


def _spec_from_fixture(symbol_key: str, op_level: str,
                       ladder: List[str], market: str) -> Optional[dict]:
    """从 fixture 组一个套件 spec。"""
    klines: Dict[str, pd.DataFrame] = {}
    for level in ladder:
        p = fixture_path(symbol_key, level)
        if p.is_file():
            klines[level] = _norm_klines(load_klines(p), _DEFAULT_MAX_BARS)
    if op_level not in klines:
        return None
    return {"label": symbol_key, "market": market, "code": symbol_key,
            "operation_level": op_level, "level_ladder": ladder,
            "op_klines": klines[op_level], "ladder_klines": klines}


def build_default_suite_specs() -> List[dict]:
    """默认套件：QMT 实盘 A 股篮子 + fixture 美股。"""
    ladder = ["d", "30m", "5m"]
    specs: List[dict] = []
    for code in _DEFAULT_A_BASKET:
        print(f"拉取实盘 a:{code} ...")
        spec = _spec_from_live("a", code, "30m", ladder)
        if spec is not None:
            specs.append(spec)
        else:
            print(f"  [skip] a:{code} 梯队数据不全")
    for fx in ("us_TSLA_US",):
        spec = _spec_from_fixture(fx, "30m", ladder, "us")
        if spec is not None:
            specs.append(spec)
    return specs


def main(argv: Optional[List[str]] = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    mode = argv[0] if argv else "a_SZ_301004"
    if mode == "suite":
        print("=== 构建多标的验证套件（拉取实盘行情，耗时较久）===")
        specs = build_default_suite_specs()
        if not specs:
            print("套件无可用标的，退出。")
            return 1
        report = run_validation_suite(specs)
        print(format_suite_report(report))
    else:
        report = run_fixture_validation(symbol_key=mode)
        print(format_report(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
